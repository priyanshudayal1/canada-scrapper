from playwright.sync_api import sync_playwright
import os
import re
import time
import random
import io
import boto3
import requests
import tempfile
import whisper
from dotenv import load_dotenv
import platform
from PIL import Image
import json
import logging

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
	level=logging.INFO,
	format='%(asctime)s - %(levelname)s - %(message)s',
	datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

BASE_URL = "https://www.canlii.org"
START_URL = "https://www.canlii.org/ca"
SECTION_TITLE = "Boards and Tribunals"
WAIT_MS = 2000

# Bedrock CAPTCHA solver configuration
BEDROCK_MODEL_ID = "qwen.qwen3-vl-235b-a22b"  # Qwen model for vision tasks
BEDROCK_REGION = os.getenv("AWS_REGION", "us-east-1")
MAX_CAPTCHA_ATTEMPTS = 50  # Maximum attempts to solve CAPTCHA
S3_BUCKET_NAME = "can-judgements"
TRACKING_FILE = "boards_tracking.json"

# Access restriction cooldown settings
ACCESS_RESTRICTED_WAIT_MIN = 10  # Minimum wait time in minutes
ACCESS_RESTRICTED_WAIT_MAX = 20  # Maximum wait time in minutes


def is_access_restricted_page(page):
	"""Check if the page shows an access restricted/IP blocked message"""
	try:
		access_restricted_indicators = [
			"text=Access Denied",
			"text=access denied",
			"text=Access Restricted",
			"text=access restricted",
			"text=temporarily blocked",
			"text=temporarily restricted",
			"text=Too many requests",
			"text=too many requests",
			"text=rate limit",
			"text=Rate Limit",
			"text=blocked due to",
			"text=IP has been blocked",
			"text=IP address has been",
			"text=automated access",
			"text=unusual activity",
			"text=suspicious activity",
			"text=Please try again later",
			"text=come back later",
		]
		
		for indicator in access_restricted_indicators:
			try:
				if page.locator(indicator).count() > 0:
					logger.warning(f"🚫 Access restriction detected via: {indicator}")
					return True
			except:
				continue
		
		# Also check page content for common blocking messages
		try:
			body_text = page.locator("body").inner_text().lower()
			blocking_phrases = [
				"access denied",
				"access restricted", 
				"temporarily blocked",
				"too many requests",
				"rate limit exceeded",
				"ip has been blocked",
				"ip address has been blocked",
				"automated access detected",
				"unusual activity detected",
			]
			for phrase in blocking_phrases:
				if phrase in body_text:
					logger.warning(f"🚫 Access restriction detected in body: '{phrase}'")
					return True
		except:
			pass
		
		return False
	except:
		return False


def is_datadome_access_restricted(page):
	"""Check if DataDome is showing access restricted message (not a solvable CAPTCHA)"""
	try:
		for frame in page.frames:
			try:
				# Check for "Access is temporarily restricted" message
				human_title = frame.locator(".captcha__human__title")
				if human_title.count() > 0:
					title_text = human_title.inner_text().lower()
					if "temporarily restricted" in title_text or "access" in title_text:
						logger.warning("🚫 DataDome ACCESS RESTRICTED detected in frame")
						return True
				# Also check robot warning for unusual activity
				robot_warning = frame.locator(".captcha__robot__warning")
				if robot_warning.count() > 0:
					warning_text = robot_warning.inner_text().lower()
					if "unusual activity" in warning_text or "automated" in warning_text:
						# check if there are solvable elements
						audio_btn = frame.locator("#captcha__audio__button")
						slider = frame.locator(".sliderContainer, #captcha__slider")
						
						if audio_btn.count() == 0 and slider.count() == 0:
							logger.warning("🚫 DataDome unusual activity warning detected (no solvable elements)")
							return True
						# If we have solvable elements, ignore the warning text

				# Check if DataDome CAPTCHA container exists but audio button is missing
				# This indicates an unsolvable access restriction page
				captcha_container = frame.locator("#captcha-container, .captcha-container")
				audio_button = frame.locator("#captcha__audio__button")
				slider_container = frame.locator(".sliderContainer")
				if captcha_container.count() > 0:
					# If container exists but no audio button AND no slider, it's access restricted
					if audio_button.count() == 0 and slider_container.count() == 0:
						print(f"    \U0001F6AB DataDome CAPTCHA container found but no solvable elements - ACCESS RESTRICTED")
						return True
			except:
				continue
		return False
	except:
		return False


def wait_for_ip_cooldown(page, reason="access restriction"):
	"""Wait for 10-15 minutes to let IP restriction clear"""
	wait_minutes = random.randint(ACCESS_RESTRICTED_WAIT_MIN, ACCESS_RESTRICTED_WAIT_MAX)
	wait_seconds = wait_minutes * 60
	
	logger.warning("\n" + "="*60)
	logger.warning("🚫 ACCESS RESTRICTED - IP COOLDOWN REQUIRED")
	logger.warning("="*60)
	logger.warning(f"Reason: {reason}")
	logger.info(f"Waiting for {wait_minutes} minutes to let IP restriction clear...")
	logger.info(f"Start time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
	logger.info(f"Resume time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + wait_seconds))}")
	logger.warning("="*60 + "\n")
	
	# Wait with countdown updates every minute
	for remaining_minutes in range(wait_minutes, 0, -1):
		print(f"    ⏳ {remaining_minutes} minute(s) remaining...")
		# Wait 1 minute (60 seconds)
		for _ in range(12):  # 12 * 5 seconds = 60 seconds
			page.wait_for_timeout(5000)
	
	logger.info("\n" + "="*60)
	logger.info("✅ IP COOLDOWN COMPLETE - Resuming operations")
	logger.info("="*60 + "\n")
	
	return True


def get_firefox_launch_args():
	"""Get robust Firefox arguments for evasion"""
	return []


def get_firefox_user_prefs():
	"""Get Firefox user preferences for stealth"""
	return {
		"dom.webdriver.enabled": False,
		"useSystemGlobalMediaControls": False,
		"marionette.enabled": False,
		"general.useragent.override": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
		"general.appname.override": "Netscape",
		"general.appversion.override": "5.0 (Windows)",
		"general.platform.override": "Win32",
		"general.oscpu.override": "Windows NT 10.0; Win64; x64",
		"privacy.resistFingerprinting": False,
		"network.cookie.cookieBehavior": 0,
		"toolkit.telemetry.enabled": False,
		"datareporting.healthreport.uploadEnabled": False,
	}


def get_stealth_scripts():
	"""Get list of JavaScripts to inject for evasion"""
	return [
		# Override navigator.webdriver
		"Object.defineProperty(navigator, 'webdriver', {get: () => undefined})",
		
		# Mock permissions
		"""
		const originalQuery = window.navigator.permissions.query;
		window.navigator.permissions.query = (parameters) => (
			parameters.name === 'notifications' ?
			Promise.resolve({ state: 'denied' }) :
			originalQuery(parameters)
		);
		""",
		
		# Mock languages
		"""
		Object.defineProperty(navigator, 'languages', {
			get: () => ['en-US', 'en'],
		});
		""",

		# Mock hardware properties
		"""
		Object.defineProperty(navigator, 'hardwareConcurrency', {
			get: () => 4,
		});
		""",

		# Mock device memory
		"""
		Object.defineProperty(navigator, 'deviceMemory', {
			get: () => 8,
		});
		"""
	]


def is_captcha_page(page):
	"""Check if the current page is a CAPTCHA page (CanLII or DataDome) or access restricted"""
	try:
		# Check for access restriction first
		if is_access_restricted_page(page):
			return True
		
		# Check for CanLII CAPTCHA elements
		captcha_indicators = [
			"text=Dear User",
			"text=please proceed with our captcha test",
			"#captchaForm",
			"#captchaTag",
			"text=Happy Searching!",
			"#captchaTest"
		]
		
		for indicator in captcha_indicators:
			if page.locator(indicator).count() > 0:
				return True
		
		# Check for DataDome CAPTCHA
		if is_datadome_captcha(page):
			return True
		
		return False
	except:
		return False


def is_datadome_captcha(page, silent=False):
	"""Check if the current page has a DataDome CAPTCHA, checking all frames"""
	try:
		datadome_indicators = [
			"#captcha-container",
			"#ddv1-captcha-container",
			"#captcha__frame",
			"#captcha__audio__button",
			".captcha__human",
			".captcha__human__title",
			"[data-dd-captcha-container]",
			"text=Verification Required",
			"text=Slide right to secure your access",
			".sliderContainer",
		]
		
		# Check main page and all frames (with limit to prevent hanging)
		frames_to_check = page.frames[:10]  # Limit to first 10 frames
		for frame in frames_to_check:
			for indicator in datadome_indicators:
				try:
					if frame.locator(indicator).count() > 0:
						if not silent:
							logger.info(f"🔴 DataDome detected via: {indicator}")
						return frame
				except:
					continue
		
		return None
	except:
		return None


def solve_datadome_audio_captcha(page):
	"""Solve DataDome audio CAPTCHA by transcribing numbers"""
	logger.info("\n🎧 Attempting to solve DataDome audio CAPTCHA...")
	
	try:
		# Find the frame containing the CAPTCHA (silent to avoid repeated logging)
		captcha_frame = is_datadome_captcha(page, silent=True)
		if not captcha_frame:
			# Fallback to main page if not found (though it should be)
			captcha_frame = page
			
		# Wait for the captcha container to load
		try:
			# Check for common containers instead of the iframe id itself
			captcha_frame.wait_for_selector("#captcha-container, .captcha-container, #captcha__audio__button", timeout=10000)
		except:
			logger.warning("⚠️  Timeout waiting for captcha elements")
			return False
		
		# Click on audio button to switch to audio mode
		audio_button = captcha_frame.locator("#captcha__audio__button")
		if audio_button.count() > 0:
			# Check if already active
			is_active = False
			try:
				if "toggled" in audio_button.get_attribute("class", ""):
					is_active = True
				if audio_button.get_attribute("aria-expanded") == "true":
					is_active = True
			except:
				pass
			
			if is_active:
				logger.info("Audio mode already active, skipping click...")
			else:
				logger.info("Clicking audio button...")
				audio_button.click()
				page.wait_for_timeout(1500)
		
		# Wait for audio mode to be active
		try:
			captcha_frame.wait_for_selector("#captcha__audio.toggled", timeout=5000)
		except:
			# Try clicking again if it didn't switch
			if audio_button.count() > 0:
				audio_button.click()
				page.wait_for_timeout(1500)
		
		# Get the audio URL
		audio_element = captcha_frame.locator("audio.audio-captcha-track")
		if audio_element.count() == 0:
			logger.warning("⚠️  Audio element not found")
			return False
		
		audio_url = audio_element.get_attribute("src")
		if not audio_url:
			logger.warning("⚠️  Audio URL not found")
			return False
		
		logger.info(f"📥 Downloading audio from: {audio_url[:50]}...")
		
		# Download the audio file
		try:
			response = requests.get(audio_url, timeout=30)
			if response.status_code != 200:
				logger.error(f"⚠️  Failed to download audio: {response.status_code}")
				return False
			
			audio_data = response.content
		except Exception as e:
			logger.error(f"⚠️  Error downloading audio: {e}")
			return False
		
		# Transcribe using AWS Transcribe
		numbers = transcribe_audio_captcha(audio_data)
		
		if not numbers or len(numbers) != 6:
			logger.warning(f"⚠️  Failed to get 6 digits, got: {numbers}")
			return False
		
		logger.info(f"🔢 Transcribed numbers: {numbers}")
		
		# Fill in the 6 input fields
		inputs = captcha_frame.locator(".audio-captcha-inputs").all()
		if len(inputs) != 6:
			try:
				# Sometimes inputs load slowly
				page.wait_for_timeout(1000)
				inputs = captcha_frame.locator(".audio-captcha-inputs").all()
			except:
				pass
				
			if len(inputs) != 6:
				logger.warning(f"⚠️  Expected 6 inputs, found {len(inputs)}")
				return False
		
		for i, digit in enumerate(numbers):
			inputs[i].fill(str(digit))
			page.wait_for_timeout(100)
		
		logger.info("✅ Filled in all digits, submitting...")
		
		# Click verify button
		page.wait_for_timeout(500)
		verify_button = captcha_frame.locator(".audio-captcha-submit-button")
		if verify_button.count() > 0:
			verify_button.click()
			page.wait_for_timeout(3000)
		
		# Check if CAPTCHA was solved
		if not is_datadome_captcha(page):
			logger.info("✅ DataDome CAPTCHA solved successfully!")
			return True
		else:
			logger.warning("❌ CAPTCHA still present, may need to retry...")
			return False
		
	except Exception as e:
		logger.error(f"⚠️  Error solving DataDome CAPTCHA: {e}")
		return False


def force_remove_cookie_modal(page):
	"""Aggressively remove cookie consent modal and backdrop"""
	try:
		page.evaluate("""
			const idsToRemove = [
				'cookieConsentBlocker',
				'cookieConsentBanner',
				'cookieConsentModal',
				'cookieConsentContainer'
			];
			idsToRemove.forEach(id => {
				const el = document.getElementById(id);
				if (el) el.remove();
			});
			const backdrops = document.querySelectorAll('.modal-backdrop');
			backdrops.forEach(el => el.remove());
			document.body.classList.remove('modal-open');
			document.body.style.overflow = 'auto';
		""")
	except:
		pass


def solve_canlii_audio_captcha(page):
	"""Solve CanLII standard audio CAPTCHA"""
	logger.info("\n🎧 Attempting to solve CanLII audio CAPTCHA...")
	
	# Ensure blocking elements are gone
	force_remove_cookie_modal(page)
	
	try:
		# Check if audio is already visible
		audio_tag = page.locator("#audioCaptchaTag")
		needs_toggle = True
		
		# Check visibility properly
		if audio_tag.count() > 0:
			# If element exists, check style or visibility
			if audio_tag.is_visible():
				logger.info("Audio tag already visible.")
				needs_toggle = False
		
		if needs_toggle:
			# Locate the audio toggle button
			audio_toggle = page.locator("#toggleAudio")
			if audio_toggle.count() == 0:
				logger.warning("⚠️  Audio toggle button not found")
				return False
				
			logger.info("Clicking audio toggle button...")
			try:
				# Force click to bypass any remaining overlays
				audio_toggle.click(force=True, timeout=5000)
				page.wait_for_timeout(1000)
			except Exception as e:
				logger.warning(f"⚠️  Click audio toggle failed ({e}). Attempting JS click...")
				try:
					page.evaluate("document.getElementById('toggleAudio').click()")
					page.wait_for_timeout(1000)
				except Exception as js_e:
					logger.warning(f"⚠️  JS audio toggle also failed: {js_e}")
					return False
			
		# Re-locate audio tag
		audio_tag = page.locator("#audioCaptchaTag")
		if audio_tag.count() == 0:
			logger.warning("⚠️  Audio tag not found")
			return False
			
		# Wait specifically for src attribute
		logger.info("Waiting for audio source...")
		try:
			# simple wait logic or loop
			for _ in range(10):
				src = audio_tag.get_attribute("src")
				if src:
					break
				page.wait_for_timeout(500)
		except:
			pass
			
		audio_src = audio_tag.get_attribute("src")
		if not audio_src:
			logger.warning("⚠️  Audio source not found")
			return False
			
		full_audio_url = BASE_URL + audio_src if audio_src.startswith("/") else audio_src
		logger.info(f"📥 Downloading audio from: {full_audio_url[:50]}...")
		
		# Download audio with headers to avoid 403
		cookies = get_cookies_dict(page)
		user_agent = page.evaluate("navigator.userAgent")
		headers = {
			"User-Agent": user_agent,
			"Referer": page.url
		}
		
		response = requests.get(full_audio_url, headers=headers, cookies=cookies, timeout=30)
		
		if response.status_code != 200:
			logger.error(f"⚠️  Failed to download audio: {response.status_code}")
			return False
			
		audio_data = response.content
		
		# Transcribe
		numbers = transcribe_audio_captcha(audio_data)
		if not numbers:
			logger.warning("⚠️  Transcription failed")
			try:
				# If transcription fails, we might still be in audio mode.
				# We should leave it to the caller (solve_captcha_automatically) to reset state
				# or we reset it here. Let's do nothing and let caller handle fallback.
				pass
			except:
				pass
			return False
			
		logger.info(f"🔢 Transcribed text: {numbers}")
		
		# Fill response
		captcha_input = page.locator("#captchaResponse")
		captcha_input.fill(numbers)
		
		# Submit
		logger.info("Submitted answer, clicking ok...")
		try:
			page.locator("input[type='submit'][value='ok']").click(timeout=5000)
		except Exception as e:
			logger.warning(f"⚠️  Submit click failed: {e}. Trying JS...")
			page.evaluate("document.querySelector('input[type=\"submit\"][value=\"ok\"]').click()")
		
		page.wait_for_timeout(3000)
		
		if not is_captcha_page(page):
			logger.info("✅ CanLII Audio CAPTCHA solved successfully!")
			return True
		else:
			logger.warning("❌ CAPTCHA solution incorrect")
			return False
			
	except Exception as e:
		logger.error(f"⚠️  Error solving CanLII audio CAPTCHA: {e}")
		return False


def transcribe_audio_captcha(audio_data):
	"""Transcribe audio CAPTCHA using local Whisper model"""
	temp_path = None
	try:
		# Save audio to temp file
		with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
			f.write(audio_data)
			temp_path = f.name
		
		print("    ⏳ Loading Whisper model (base)...")
		# Load the model (this will download it on first run - approx 140MB)
		model = whisper.load_model("base")
		
		# Suppress FP16 warning on CPU
		import warnings
		warnings.filterwarnings("ignore", message="FP16 is not supported on CPU")
		
		logger.info("Transcribing audio...")
		result = model.transcribe(temp_path)
		transcript = result["text"]
		
		# Extract only digits
		numbers = re.sub(r'[^0-9]', '', transcript)
		
		# Clean up
		if temp_path and os.path.exists(temp_path):
			os.unlink(temp_path)
			
		return numbers
			
	except Exception as e:
		logger.error(f"⚠️  Transcription error: {e}")
		if temp_path and os.path.exists(temp_path):
			try:
				os.unlink(temp_path)
			except:
				pass
		return None


def initialize_bedrock_client():
	"""Initialize AWS Bedrock client for CAPTCHA solving"""
	try:
		aws_key = os.getenv("AWS_ACCESS_KEY_ID")
		aws_secret = os.getenv("AWS_SECRET_ACCESS_KEY")
		
		if not aws_key or not aws_secret:
			logger.warning("⚠️  AWS credentials not found for Bedrock")
			return None
		
		bedrock_client = boto3.client(
			"bedrock-runtime",
			region_name=BEDROCK_REGION,
			aws_access_key_id=aws_key,
			aws_secret_access_key=aws_secret,
		)
		return bedrock_client
	except Exception as e:
		logger.error(f"⚠️  Failed to initialize Bedrock client: {e}")
		return None


def solve_captcha_with_bedrock(image_bytes):
	"""Solve CAPTCHA using AWS Bedrock vision model"""
	bedrock_client = initialize_bedrock_client()
	if not bedrock_client:
		return ""
	
	try:
		# Determine image format
		image = Image.open(io.BytesIO(image_bytes))
		image_format = (image.format or "PNG").lower()
		if image_format == "jpg":
			image_format = "jpeg"
		
		messages = [
			{
				"role": "user",
				"content": [
					{"image": {"format": image_format, "source": {"bytes": image_bytes}}},
					{
						"text": (
							"Read the captcha text in this image. Only output the exact characters you see, "
							"nothing else. The captcha contains alphanumeric characters. Do not include any spaces or special characters."
						)
					},
				],
			}
		]
		
		response = bedrock_client.converse(
			modelId=BEDROCK_MODEL_ID,
			messages=messages,
			inferenceConfig={"maxTokens": 50, "temperature": 0},
		)
		
		# Extract response text
		out = ""
		try:
			out = response["output"]["message"]["content"][0]["text"]
		except Exception:
			try:
				out = response.get("body", "")
			except Exception:
				out = ""
		
		# Clean the response - keep only alphanumeric characters
		captcha_text = re.sub(r"[^A-Za-z0-9]", "", str(out))
		return captcha_text.strip()
	except Exception as e:
		logger.error(f"⚠️  Bedrock CAPTCHA solving failed: {e}")
		return ""


def solve_captcha_automatically(page):
	"""Attempt to automatically solve the CAPTCHA on the page"""
	logger.info("\n🤖 Attempting automatic CAPTCHA solving...")

	# Remove cookie consent blocker if present
	force_remove_cookie_modal(page)
	
	# First, check for DataDome CAPTCHA (slider/audio type)
	if is_datadome_captcha(page):
		# CRITICAL: Check if this is an "Access Restricted" variant (no solvable CAPTCHA)
		if is_datadome_access_restricted(page):
			logger.warning("🚫 This is a DataDome ACCESS RESTRICTED page - NOT a solvable CAPTCHA!")
			logger.warning("🚫 IP has been rate-limited. Triggering cooldown...")
			wait_for_ip_cooldown(page, reason="DataDome Access Restricted - IP rate-limited after high download volume")
			# After cooldown, check if access is restored
			page.goto(START_URL, wait_until="commit")
			page.wait_for_load_state("domcontentloaded")
			page.wait_for_timeout(3000)
			# Check again - might need another cooldown or regular CAPTCHA
			if is_datadome_access_restricted(page):
				logger.warning("⚠️  Still access restricted after cooldown, waiting again...")
				wait_for_ip_cooldown(page, reason="Still access restricted after first cooldown")
				page.goto(START_URL, wait_until="commit")
				page.wait_for_load_state("domcontentloaded")
			# Now check if there's a regular CAPTCHA or if we're clear
			if not is_captcha_page(page):
				logger.info("✅ Access restored after cooldown!")
				return True
			else:
				# There might be a regular CAPTCHA now, recurse
				return solve_captcha_automatically(page)
		
		logger.info("📌 Detected DataDome CAPTCHA (slider/audio type)")
		for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
			logger.info(f"DataDome attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")
			if solve_datadome_audio_captcha(page):
				return True
			# Check if it became access restricted during attempts
			if is_datadome_access_restricted(page):
				logger.warning("🚫 CAPTCHA attempts triggered access restriction!")
				wait_for_ip_cooldown(page, reason="Access restricted after CAPTCHA solve attempts")
				page.goto(START_URL, wait_until="commit")
				page.wait_for_load_state("domcontentloaded")
				return solve_captcha_automatically(page)
			# Reload captcha for next attempt
			try:
				reload_button = page.locator("#captcha__reload__button")
				if reload_button.count() > 0:
					reload_button.click()
					page.wait_for_timeout(2000)
			except:
				pass
		logger.warning("⚠️  DataDome auto-solve failed, waiting for manual input...")
		return False
	
	# Fall back to CanLII CAPTCHA
	for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
		logger.info(f"Attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")
		
		# Try Audio First
		if solve_canlii_audio_captcha(page):
			return True
			
		logger.warning("⚠️  Audio solve failed/skipped, trying Visual/Bedrock...")
		
		try:
			# Ensure we are in Visual mode (Audio mode hides the image)
			captcha_img = page.locator("#captchaTag")
			if captcha_img.count() > 0 and not captcha_img.is_visible():
				logger.info("Image hidden, toggling back to Visual mode...")
				toggle_btn = page.locator("#toggleAudio")
				if toggle_btn.count() > 0:
					toggle_btn.click(force=True)
					page.wait_for_timeout(1500)
			
			# Wait for captcha image to load
			page.wait_for_selector("#captchaTag", state="visible", timeout=5000)
			captcha_img = page.locator("#captchaTag")
			
			if captcha_img.count() == 0 or not captcha_img.is_visible():
				logger.warning("⚠️  CAPTCHA image not found or not visible")
				continue
			
			# Take screenshot of the CAPTCHA image
			image_bytes = captcha_img.screenshot()
			
			if not image_bytes:
				logger.warning("⚠️  Failed to capture CAPTCHA image")
				continue
			
			# Solve using Bedrock
			captcha_solution = solve_captcha_with_bedrock(image_bytes)
			
			if not captcha_solution:
				logger.warning("⚠️  Could not extract CAPTCHA text")
				# Refresh captcha for next attempt by reloading
				page.reload()
				page.wait_for_timeout(2000)
				continue
			
			logger.info(f"🔍 Detected CAPTCHA text: {captcha_solution}")
			
			# Enter the solution
			captcha_input = page.locator("#captchaResponse")
			captcha_input.fill(captcha_solution)
			
			# Submit the form
			submit_locator = page.locator("input[type='submit'][value='ok']")
			try:
				submit_locator.click(timeout=5000)
			except Exception as e:
				logger.warning(f"⚠️  Submit click failed ({e}). Attempting JS click...")
				try:
					page.evaluate("document.querySelector('input[type=\"submit\"][value=\"ok\"]').click()")
				except Exception as js_e:
					logger.warning(f"⚠️  JS submit also failed: {js_e}")
			
			page.wait_for_timeout(3000)
			
			# Check if CAPTCHA was solved successfully
			if not is_captcha_page(page):
				logger.info("✅ CAPTCHA solved successfully!")
				return True
			else:
				logger.warning("❌ CAPTCHA solution was incorrect, retrying...")
				# Refresh page to get a new captcha challenge
				page.reload()
				page.wait_for_timeout(2000)
				
		except Exception as e:
			logger.error(f"⚠️  Error during CAPTCHA solving: {e}")
			# Force reload to reset state if stuck
			try:
				page.reload()
				page.wait_for_timeout(2000)
			except:
				pass
			continue
	
	logger.warning("⚠️  Auto-solve failed after max attempts, waiting for manual input...")
	return False


def handle_captcha_interruption(page):
	"""
	Handle CAPTCHA detected during deep processing.
	Strategy: Check for access restriction -> Wait if needed -> Go to Homepage -> Solve -> Return True so caller can retry.
	"""
	logger.warning("\n🛑 CAPTCHA INTERRUPTION DETECTED!")
	logger.info("Initiating recovery protocol...")
	
	try:
		# Check for DataDome "Access Restricted" variant FIRST (inside iframe)
		if is_datadome_access_restricted(page):
			logger.warning("🚫 DataDome ACCESS RESTRICTED detected - IP rate-limited!")
			wait_for_ip_cooldown(page, reason="DataDome Access Restricted during scraping")
			
			# After waiting, go to homepage and check again
			logger.info(f"Navigating to homepage ({START_URL}) after cooldown...")
			page.goto(START_URL, wait_until="commit")
			page.wait_for_load_state("domcontentloaded")
			
			# If still restricted after waiting, wait again
			if is_datadome_access_restricted(page):
				print("   ⚠️  Still access restricted after first cooldown, waiting again...")
				wait_for_ip_cooldown(page, reason="DataDome still restricted after first cooldown")
				page.goto(START_URL, wait_until="commit")
				page.wait_for_load_state("domcontentloaded")
			
			# Now check for remaining CAPTCHA
			if is_captcha_page(page) and not is_datadome_access_restricted(page):
				logger.info("Found solvable CAPTCHA after cooldown. Solving...")
				if solve_captcha_automatically(page):
					print("   ✅ CAPTCHA solved after cooldown!")
					page.wait_for_timeout(2000)
					return True
				else:
					print("   ⚠️  Auto-solve failed. Waiting for manual input...")
					while is_captcha_page(page):
						page.wait_for_timeout(5000)
					print("   ✅ Manual solve detected!")
					return True
			else:
				print("   ✅ Access restored after cooldown!")
				return True
		
		# Check for regular access restriction (main page body)
		if is_access_restricted_page(page):
			print("   🚫 Access restriction detected - IP may be blocked due to high download volume")
			wait_for_ip_cooldown(page, reason="Access restriction detected during scraping")
			
			# After waiting, go to homepage and check again
			print(f"   Navigating to homepage ({START_URL}) after cooldown...")
			page.goto(START_URL, wait_until="commit")
			page.wait_for_load_state("domcontentloaded")
			
			# If still restricted after waiting, wait again
			if is_access_restricted_page(page):
				print("   ⚠️  Still restricted after first cooldown, waiting again...")
				wait_for_ip_cooldown(page, reason="Access still restricted after first cooldown")
				page.goto(START_URL, wait_until="commit")
				page.wait_for_load_state("domcontentloaded")
			
			# Now check for remaining CAPTCHA
			if is_captcha_page(page) and not is_access_restricted_page(page):
				print("   Found regular CAPTCHA after cooldown. Solving...")
				if solve_captcha_automatically(page):
					print("   ✅ CAPTCHA solved after cooldown!")
					page.wait_for_timeout(2000)
					return True
				else:
					print("   ⚠️  Auto-solve failed. Waiting for manual input...")
					while is_captcha_page(page):
						page.wait_for_timeout(5000)
					print("   ✅ Manual solve detected!")
					return True
			else:
				print("   ✅ Access restored after cooldown!")
				return True
		
		# 1. Go to homepage (safest place to solve)
		print(f"   Navigating to homepage ({START_URL}) to solve...")
		page.goto(START_URL, wait_until="commit")
		page.wait_for_load_state("domcontentloaded")
		
		# Check if homepage also shows access restriction
		if is_access_restricted_page(page):
			print("   🚫 Homepage also shows access restriction")
			wait_for_ip_cooldown(page, reason="Access restriction on homepage")
			page.goto(START_URL, wait_until="commit")
			page.wait_for_load_state("domcontentloaded")
		
		# 2. Solve it
		if is_captcha_page(page):
			print("   Found CAPTCHA on homepage. Solving...")
			if solve_captcha_automatically(page):
				print("   ✅ Recovery CAPTCHA solved!")
				page.wait_for_timeout(2000)
				return True
			else:
				print("   ⚠️  Auto-solve failed during recovery. Waiting for manual input...")
				# Wait manually
				while is_captcha_page(page):
					page.wait_for_timeout(5000)
				print("   ✅ Manual solve detected!")
				return True
		else:
			print("   ❓ No CAPTCHA found on homepage? Maybe it cleared itself.")
			return True
			
	except Exception as e:
		print(f"   ❌ Recovery failed: {e}")
		return False


def collect_links(page, section_title):
	section = page.locator("section", has=page.locator("h2", has_text=section_title))
	hrefs = section.locator("a.canlii").evaluate_all("els=>els.map(e=>e.getAttribute('href'))")
	return [href for href in hrefs if href]


def download_pdf(url, output_path, cookies, user_agent):
	"""Download PDF file using requests with cookies"""
	try:
		headers = {
			"User-Agent": user_agent,
			"Referer": "https://www.canlii.org/"
		}
		response = requests.get(url, headers=headers, cookies=cookies, timeout=60, stream=True)
		if response.status_code == 200:
			os.makedirs(os.path.dirname(output_path), exist_ok=True)
			with open(output_path, 'wb') as f:
				for chunk in response.iter_content(chunk_size=8192):
					f.write(chunk)
			print(f"    ✅ Downloaded: {output_path}")
			return True
		else:
			print(f"    ❌ Failed to download {url}: Status {response.status_code}")
			return False
	except Exception as e:
		logger.error(f"❌ Download error: {e}")
		return False


def sanitize_filename(filename):
	"""Remove invalid characters from filename"""
	return re.sub(r'[<>:"/\\|?*]', '_', filename)


def file_exists_in_s3(s3_key):
	"""Check if a file already exists in S3 bucket"""
	try:
		s3_client = boto3.client(
			's3',
			aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
			aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
			region_name=os.getenv('AWS_REGION', 'us-east-1')
		)
		
		# Check if object exists
		s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
		return True
	except Exception:
		return False


def upload_to_s3(local_file_path, s3_key):
	"""Upload a file to S3 bucket"""
	try:
		# Check if file already exists in S3
		if file_exists_in_s3(s3_key):
			logger.info(f"⏭️  Already in S3: s3://{S3_BUCKET_NAME}/{s3_key}")
			return True  # Return True so local file gets deleted
		
		s3_client = boto3.client(
			's3',
			aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
			aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
			region_name=os.getenv('AWS_REGION', 'us-east-1')
		)
		
		# Upload the file
		s3_client.upload_file(local_file_path, S3_BUCKET_NAME, s3_key)
		logger.info(f"✓ Uploaded to S3: s3://{S3_BUCKET_NAME}/{s3_key}")
		return True
	except Exception as e:
		logger.error(f"✗ S3 upload failed: {e}")
		return False


def delete_local_file(file_path):
	"""Delete a local file after successful upload"""
	try:
		if os.path.exists(file_path):
			os.remove(file_path)
			logger.info(f"🗑️  Deleted local file: {os.path.basename(file_path)}")
			return True
	except Exception as e:
		logger.warning(f"⚠️  Could not delete local file: {e}")
		return False


def load_tracking_data():
	"""Load tracking data from JSON file"""
	if os.path.exists(TRACKING_FILE):
		try:
			with open(TRACKING_FILE, 'r', encoding='utf-8') as f:
				data = json.load(f)
				return data
		except Exception as e:
			print(f"Warning: Could not load tracking file: {e}")
			return {"processed_documents": []}
	return {"processed_documents": []}


def save_tracking_data(tracking_data):
	"""Save tracking data to JSON file"""
	try:
		with open(TRACKING_FILE, 'w', encoding='utf-8') as f:
			json.dump(tracking_data, f, indent=2, ensure_ascii=False)
	except Exception as e:
		logger.error(f"Warning: Could not save tracking file: {e}")


def is_already_processed(tracking_data, document_key):
	"""Check if a document has already been processed"""
	existing_keys = [d.get("url") for d in tracking_data.get("processed_documents", [])]
	return document_key in existing_keys


def mark_as_processed(tracking_data, doc_info):
	"""Mark a document as processed with detailed info and save"""
	if not is_already_processed(tracking_data, doc_info.get("url")):
		if "processed_documents" not in tracking_data:
			tracking_data["processed_documents"] = []
		tracking_data["processed_documents"].append(doc_info)
		save_tracking_data(tracking_data)


def get_cookies_dict(page):
	"""Get cookies from Playwright context as a dictionary"""
	cookies = page.context.cookies()
	cookie_dict = {}
	for cookie in cookies:
		cookie_dict[cookie['name']] = cookie['value']
	return cookie_dict


def process_decision_page(page, decision_url, save_dir, tracking_data):
	"""Process individual decision page and download PDF"""
	try:
		if is_already_processed(tracking_data, decision_url):
			logger.info(f"⏭️  Skipping (already processed): {decision_url.split('/')[-1]}")
			return

		# print(f"  Processing decision: {decision_url}")
		page.goto(decision_url, wait_until="domcontentloaded")
		
		# Check for CAPTCHA
		if is_captcha_page(page):
			if handle_captcha_interruption(page):
				page.goto(decision_url, wait_until="domcontentloaded")
			else:
				return
		
		# Extract title for filename
		title_el = page.locator("h1.main-title")
		if title_el.count() > 0:
			doc_title = title_el.inner_text().strip()
		else:
			doc_title = decision_url.split("/")[-1]

		# Sanitize title
		safe_title = sanitize_filename(doc_title)[:200]
		s3_key = f"{safe_title}.pdf"

		# Find PDF link
		pdf_link_loc = page.locator("#pdf-link")
		if pdf_link_loc.count() > 0:
			pdf_href = pdf_link_loc.get_attribute("href")
			if pdf_href:
				full_pdf_url = BASE_URL + pdf_href if pdf_href.startswith("/") else pdf_href
				output_path = os.path.join(save_dir, s3_key)
				
				# Download
				cookies = get_cookies_dict(page)
				user_agent = page.evaluate("navigator.userAgent")
				if download_pdf(full_pdf_url, output_path, cookies, user_agent):
					# Upload to S3
					if upload_to_s3(output_path, s3_key):
						delete_local_file(output_path)
						
						# Mark as processed
						mark_as_processed(tracking_data, {
							"url": decision_url,
							"title": doc_title,
							"pdf_url": full_pdf_url,
							"s3_key": s3_key,
							"downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S")
						})
		else:
			pass # Silent skip if no PDF
			
	except Exception as e:
		print(f"    Error processing decision: {e}")


def process_year_page(page, year_url, board_name, year, tracking_data):
	"""Process a specific year page for a board/tribunal"""
	try:
		full_year_url = BASE_URL + year_url if year_url.startswith("/") else year_url
		logger.info(f"Visiting Year: {year}")
		page.goto(full_year_url, wait_until="domcontentloaded")
		
		# Check CAPTCHA
		if is_captcha_page(page):
			if handle_captcha_interruption(page):
				page.goto(full_year_url, wait_until="domcontentloaded")
			else:
				return

		# Process rows
		try:
			page.wait_for_selector("#decisionsListing", timeout=10000)
		except:
			logger.warning(f"⚠️ No decisions table found for {year}")
			return

		rows = page.locator("#decisionsListing tr").all()
		logger.info(f"Found {len(rows)} decisions")
		
		# Collect decision URLs first
		decision_links = []
		for row in rows:
			link_loc = row.locator("a.canlii")
			if link_loc.count() > 0:
				href = link_loc.get_attribute("href")
				if href:
					decision_links.append(BASE_URL + href if href.startswith("/") else href)
		
		save_dir = os.path.join("downloads", board_name, year)
		
		# Filter out already processed links to resume directly
		unprocessed_links = [url for url in decision_links if not is_already_processed(tracking_data, url)]
		
		if len(unprocessed_links) < len(decision_links):
			logger.info(f"⏭️  Skipping {len(decision_links) - len(unprocessed_links)} previously processed documents")
		
		for idx, url in enumerate(unprocessed_links):
			logger.info(f"[{idx+1}/{len(unprocessed_links)}] Processing decision...")
			process_decision_page(page, url, save_dir, tracking_data)
			page.wait_for_timeout(500) # Small delay
			
	except Exception as e:
		logger.error(f"Error processing year {year}: {e}")


def process_tribunal(page, tribunal_url, tracking_data):
	"""Process a board/tribunal main page"""
	try:
		logger.info(f"\nProcessing Tribunal: {tribunal_url}")
		page.goto(tribunal_url, wait_until="domcontentloaded")
		
		if is_captcha_page(page):
			if handle_captcha_interruption(page):
				page.goto(tribunal_url, wait_until="domcontentloaded")
			else:
				return
		
		page.wait_for_timeout(1000)
		
		# Find 'more, by year'
		more_link = page.locator("a", has_text="more, by year")
		if more_link.count() > 0:
			more_href = more_link.get_attribute("href")
			full_more_url = BASE_URL + more_href if more_href.startswith("/") else more_href
			print(f"  Found 'more, by year' link")
			
			# Go to nav page
			page.goto(full_more_url, wait_until="domcontentloaded")
			
			# Wait for selector
			try:
				page.wait_for_selector("#navYearsSelector", timeout=10000)
			except:
				print("    ⚠️ Years selector not found")
				return
			
			# Get years
			options = page.locator("#navYearsSelector option").all()
			print(f"  Found {len(options)} years available")
			
			# Collect year data
			years_data = []
			for opt in options:
				val = opt.get_attribute("value")
				text = opt.inner_text().strip()
				if val:
					years_data.append((text, val))
			
			board_name = tribunal_url.rstrip("/").split("/")[-1]
			
			# Process each year
			for year_text, year_val in years_data:
				process_year_page(page, year_val, board_name, year_text, tracking_data)
				
		else:
			print("  ⚠️ 'more, by year' link not found")
			
	except Exception as e:
		print(f" Error processing tribunal: {e}")


def main():
	# Load tracking data
	tracking_data = load_tracking_data()
	logger.info(f"Loaded tracking data: {len(tracking_data.get('processed_documents', []))} documents already processed")

	with sync_playwright() as p:
		# Determine headless mode
		system_os = platform.system()
		env_headless = os.getenv("HEADLESS")
		
		if env_headless is not None:
			is_headless = env_headless.lower() == "true"
		else:
			# Default to headless on Linux, headed on Windows
			is_headless = system_os == "Linux"
			
		logger.info(f"Running on {system_os}, Headless: {is_headless}")
		
		browser = p.firefox.launch(
			headless=is_headless,
			args=get_firefox_launch_args(),
			firefox_user_prefs=get_firefox_user_prefs()
		)
		
		context = browser.new_context(
			viewport={"width": 1920, "height": 1080},
			user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
			locale="en-US",
			timezone_id="America/Toronto",
			permissions=["geolocation"],
			geolocation={"latitude": 45.4215, "longitude": -75.6972} # Ottawa
		)
		
		# Inject all stealth scripts
		for script in get_stealth_scripts():
			context.add_init_script(script)

		page = context.new_page()
		
		# Add random mouse movement
		page.mouse.move(random.randint(100, 500), random.randint(100, 500))
		
		page.goto(START_URL, wait_until="load")
		
		# Initial CAPTCHA Check
		logger.info("\n🔍 Checking for CAPTCHA on initial page...")
		datadome_detected = is_datadome_captcha(page)
		canlii_detected = page.locator("#captchaTag").count() > 0
		logger.info(f"DataDome CAPTCHA: {'DETECTED' if datadome_detected else 'not found'}")
		logger.info(f"CanLII CAPTCHA: {'DETECTED' if canlii_detected else 'not found'}")
		
		if datadome_detected or canlii_detected or is_captcha_page(page):
			logger.warning("\n⚠️  CAPTCHA detected on initial page!")
			auto_solved = solve_captcha_automatically(page)
			if not auto_solved:
				logger.info("Please solve the CAPTCHA in the browser window...")
				while is_captcha_page(page):
					page.wait_for_timeout(5000)
				logger.info("✅ CAPTCHA solved! Continuing...")
			page.wait_for_timeout(3000)
			
			# Wait briefly to see if page auto-reloads
			logger.info("Waiting for page to stabilize...")
			page.wait_for_timeout(5000)
			
			# Check if we are already on the page with content
			if page.locator("h2", has_text=SECTION_TITLE).count() > 0:
				logger.info("Page content appears loaded, skipping reload.")
			else:
				logger.info("Reloading page explicitly...")
				try:
					page.goto(START_URL, wait_until="commit", timeout=60000)
					try:
						page.wait_for_load_state("domcontentloaded", timeout=60000)
					except:
						pass
				except Exception as e:
					print(f"Warning: Navigation timeout after CAPTCHA, continuing anyway: {e}")
				
			page.wait_for_timeout(WAIT_MS)

		# Handle cookie consent with logging
		logger.info("Checking for cookie banner...")
		
		try:
			if page.locator("#cookieConsentBanner").count() > 0:
				logger.info("Cookie banner detected")
				try:
					page.evaluate("""
						const btn = document.getElementById('understandCookieConsent');
						if (btn) btn.click();
					""")
					logger.info("Cookie consent clicked successfully")
				except:
					pass
				page.wait_for_timeout(1000)
		except:
			pass
		
		page.wait_for_timeout(WAIT_MS)
		
		# Collect links
		logger.info(f"\n=== Collecting {SECTION_TITLE} links ===")
		links = collect_links(page, SECTION_TITLE)
		logger.info(f"Found {len(links)} links")
		
		if len(links) == 0:
			logger.warning("⚠️  No links found, checking for CAPTCHA...")
			if is_captcha_page(page):
				auto_solved = solve_captcha_automatically(page)
				if not auto_solved:
					logger.info("Please solve the CAPTCHA in the browser window...")
					while is_captcha_page(page):
						page.wait_for_timeout(5000)
					logger.info("✅ CAPTCHA solved! Continuing...")
				
				page.wait_for_timeout(3000)
				logger.info("Waiting for page to stabilize...")
				if page.locator("h2", has_text=SECTION_TITLE).count() > 0:
					logger.info("Page content appears loaded, skipping reload.")
				else:
					logger.info("Reloading page explicitly...")
					try:
						page.goto(START_URL, wait_until="commit", timeout=60000)
						try:
							page.wait_for_load_state("domcontentloaded", timeout=60000)
						except:
							pass
					except Exception as e:
						logger.warning(f"Navigation timeout after CAPTCHA, continuing anyway: {e}")
					
				force_remove_cookie_modal(page)
				if is_captcha_page(page):
					logger.info("⚠️  CAPTCHA encountered/persisted, attempting to solve...")
					if solve_captcha_automatically(page):
						logger.info("✅ CAPTCHA solved!")
						page.wait_for_timeout(3000)
						force_remove_cookie_modal(page)
				
				page.wait_for_timeout(WAIT_MS)
				links = collect_links(page, SECTION_TITLE)
				logger.info(f"Found {len(links)} links after retry")
		
		for i, href in enumerate(links, 1):
			logger.info(f"Visiting link {i}/{len(links)}: {href}")
			url = f"{BASE_URL}{href}"
			process_tribunal(page, url, tracking_data)
			
		browser.close()


if __name__ == "__main__":
	main()
