from playwright.sync_api import sync_playwright
import json
import os
import re
import time
import random
import io
import boto3
import requests
import tempfile
import whisper
from pathlib import Path
from dotenv import load_dotenv
import platform
from PIL import Image

# Load environment variables
load_dotenv()

BASE_URL = "https://www.canlii.org"
START_URL = "https://www.canlii.org/ca"
SECTION_TITLE = "Legislation"
WAIT_MS = 2000
OUTPUT_DIR = "legislation_pdfs"
DOWNLOAD_DELAY_MIN = 1  # Minimum delay in seconds between downloads (increased to avoid CAPTCHAs)
DOWNLOAD_DELAY_MAX = 2  # Maximum delay in seconds between downloads (increased to avoid CAPTCHAs)
S3_BUCKET_NAME = "can-bareacts"  # S3 bucket name
TRACKING_FILE = "download_tracking.json"  # File to track processed documents
SKIPPED_FILE = "skipped_documents.json"  # File to track repealed/not-in-force documents

# Bedrock CAPTCHA solver configuration
BEDROCK_MODEL_ID = "qwen.qwen3-vl-235b-a22b"  # Qwen model for vision tasks
BEDROCK_REGION = os.getenv("AWS_REGION", "us-east-1")
MAX_CAPTCHA_ATTEMPTS = 50  # Maximum attempts to solve CAPTCHA

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
					print(f"    🚫 Access restriction detected via: {indicator}")
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
					print(f"    🚫 Access restriction detected in body: '{phrase}'")
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
						print(f"    🚫 DataDome ACCESS RESTRICTED detected in frame")
						return True
				# Also check robot warning for unusual activity
				robot_warning = frame.locator(".captcha__robot__warning")
				if robot_warning.count() > 0:
					warning_text = robot_warning.inner_text().lower()
					if "unusual activity" in warning_text or "automated" in warning_text:
						print(f"    🚫 DataDome unusual activity warning detected")
						return True
				# Check if DataDome CAPTCHA container exists but audio button is missing
				# This indicates an unsolvable access restriction page
				captcha_container = frame.locator("#captcha-container, .captcha-container")
				audio_button = frame.locator("#captcha__audio__button")
				slider_container = frame.locator(".sliderContainer")
				if captcha_container.count() > 0:
					# If container exists but no audio button AND no slider, it's access restricted
					if audio_button.count() == 0 and slider_container.count() == 0:
						print(f"    🚫 DataDome CAPTCHA container found but no solvable elements - ACCESS RESTRICTED")
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
	
	print(f"\n{'='*60}")
	print(f"🚫 ACCESS RESTRICTED - IP COOLDOWN REQUIRED")
	print(f"{'='*60}")
	print(f"Reason: {reason}")
	print(f"Waiting for {wait_minutes} minutes to let IP restriction clear...")
	print(f"Start time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
	print(f"Resume time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + wait_seconds))}")
	print(f"{'='*60}\n")
	
	# Wait with countdown updates every minute
	for remaining_minutes in range(wait_minutes, 0, -1):
		print(f"    ⏳ {remaining_minutes} minute(s) remaining...")
		# Wait 1 minute (60 seconds)
		for _ in range(12):  # 12 * 5 seconds = 60 seconds
			page.wait_for_timeout(5000)
	
	print(f"\n{'='*60}")
	print(f"✅ IP COOLDOWN COMPLETE - Resuming operations")
	print(f"{'='*60}\n")
	
	return True


def get_browser_args():
	"""Get robust browser arguments for evasion"""
	return [
		"--disable-blink-features=AutomationControlled",
		"--disable-infobars",
		"--no-sandbox",
		"--disable-setuid-sandbox",
		"--disable-dev-shm-usage",
		"--disable-accelerated-2d-canvas",
		"--disable-gpu",
		"--window-size=1920,1080",
		"--start-maximized",
		"--lang=en-US,en",
		"--exclude-switches=enable-automation",
		"--disable-features=IsolateOrigins,site-per-process",
	]


def get_stealth_scripts():
	"""Get list of JavaScripts to inject for evasion"""
	return [
		# Override navigator.webdriver
		"Object.defineProperty(navigator, 'webdriver', {get: () => undefined})",
		
		# Mock chrome object
		"""
		window.chrome = {
			runtime: {}
		};
		""",
		
		# Mock permissions
		"""
		const originalQuery = window.navigator.permissions.query;
		window.navigator.permissions.query = (parameters) => (
			parameters.name === 'notifications' ?
			Promise.resolve({ state: 'denied' }) :
			originalQuery(parameters)
		);
		""",
		
		# Mock plugins
		"""
		Object.defineProperty(navigator, 'plugins', {
			get: () => [1, 2, 3, 4, 5],
		});
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


def sanitize_filename(filename):
	"""Remove invalid characters from filename"""
	return re.sub(r'[<>:"/\\|?*]', '_', filename)


def delay_between_downloads():
	"""Add a random delay between downloads to avoid triggering captchas"""
	pass
	# delay = random.uniform(DOWNLOAD_DELAY_MIN, DOWNLOAD_DELAY_MAX)
	# print(f"  Waiting {delay:.1f} seconds before next download...")
	# time.sleep(delay)


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
			print(f"  ⏭️  Already in S3: s3://{S3_BUCKET_NAME}/{s3_key}")
			return True  # Return True so local file gets deleted
		
		s3_client = boto3.client(
			's3',
			aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
			aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
			region_name=os.getenv('AWS_REGION', 'us-east-1')
		)
		
		# Upload the file
		s3_client.upload_file(local_file_path, S3_BUCKET_NAME, s3_key)
		print(f"  ✓ Uploaded to S3: s3://{S3_BUCKET_NAME}/{s3_key}")
		return True
	except Exception as e:
		print(f"  ✗ S3 upload failed: {e}")
		return False


def load_tracking_data():
	"""Load tracking data from JSON file"""
	if os.path.exists(TRACKING_FILE):
		try:
			with open(TRACKING_FILE, 'r', encoding='utf-8') as f:
				data = json.load(f)
				# Handle migration from old format (list of strings) to new format (list of objects)
				if data.get("processed_documents") and isinstance(data["processed_documents"][0], str):
					# Old format - convert to new format
					data["processed_keys"] = data["processed_documents"]
					data["processed_documents"] = []
				return data
		except Exception as e:
			print(f"Warning: Could not load tracking file: {e}")
			return {"processed_documents": [], "processed_keys": []}
	return {"processed_documents": [], "processed_keys": []}


def save_tracking_data(tracking_data):
	"""Save tracking data to JSON file"""
	try:
		with open(TRACKING_FILE, 'w', encoding='utf-8') as f:
			json.dump(tracking_data, f, indent=2, ensure_ascii=False)
	except Exception as e:
		print(f"Warning: Could not save tracking file: {e}")


def is_already_processed(tracking_data, document_key):
	"""Check if a document has already been processed"""
	# Check both old format (processed_keys) and new format (processed_documents)
	if document_key in tracking_data.get("processed_keys", []):
		return True
	existing_keys = [d.get("key") for d in tracking_data.get("processed_documents", []) if isinstance(d, dict)]
	return document_key in existing_keys


def mark_as_processed(tracking_data, doc_info):
	"""Mark a document as processed with detailed info and save"""
	doc_key = doc_info.get("key", "")
	if not is_already_processed(tracking_data, doc_key):
		if "processed_documents" not in tracking_data:
			tracking_data["processed_documents"] = []
		tracking_data["processed_documents"].append(doc_info)
		save_tracking_data(tracking_data)


def delete_local_file(file_path):
	"""Delete a local file after successful upload"""
	try:
		if os.path.exists(file_path):
			os.remove(file_path)
			print(f"  🗑️  Deleted local file: {os.path.basename(file_path)}")
			return True
	except Exception as e:
		print(f"  ⚠️  Could not delete local file: {e}")
		return False


def collect_category_links(page, section_title):
	"""Collect main legislation category links from the homepage"""
	section = page.locator("section", has=page.locator("h2", has_text=section_title))
	hrefs = section.locator("a.canlii").evaluate_all("els=>els.map(e=>e.getAttribute('href'))")
	return [href for href in hrefs if href]





def load_skipped_data():
	"""Load skipped documents data from JSON file"""
	if os.path.exists(SKIPPED_FILE):
		try:
			with open(SKIPPED_FILE, 'r', encoding='utf-8') as f:
				return json.load(f)
		except Exception as e:
			print(f"Warning: Could not load skipped file: {e}")
			return {"skipped_documents": []}
	return {"skipped_documents": []}


def save_skipped_document(doc_info):
	"""Save a skipped document to the tracking file"""
	try:
		skipped_data = load_skipped_data()
		# Check if already in list (by href)
		existing_hrefs = [d.get("href") for d in skipped_data.get("skipped_documents", [])]
		if doc_info.get("href") not in existing_hrefs:
			skipped_data["skipped_documents"].append(doc_info)
			with open(SKIPPED_FILE, 'w', encoding='utf-8') as f:
				json.dump(skipped_data, f, indent=2, ensure_ascii=False)
	except Exception as e:
		print(f"Warning: Could not save skipped document: {e}")


def is_document_in_force(page, href="", title=""):
	"""Check if the document is currently in force based on page metadata"""
	try:
		# Check for warning banners indicating repealed/spent status
		warning_elements = page.locator("#warnings .warning")
		if warning_elements.count() > 0:
			# Handle multiple warning elements by getting all texts
			warning_texts = warning_elements.all_inner_texts()
			for warning_text in warning_texts:
				warning_lower = warning_text.lower()
				if any(x in warning_lower for x in ["repealed", "spent", "not in force"]):
					print(f"    ⚠️  Document is not in force: {warning_text[:80]}...")
					# Save to skipped documents JSON
					save_skipped_document({
						"title": title,
						"href": href,
						"url": f"{BASE_URL}{href}",
						"reason": warning_text
					})
					return False
		return True
	except Exception as e:
		print(f"    Warning checking in-force status: {e}")
		return True  # Assume in force if check fails to be safe


def extract_document_content(page, href="", title=""):
	"""Extract title and structured content from a legislation document page"""
	try:
		# Check for CAPTCHA first
		if is_captcha_page(page):
			print("\n⚠️  CAPTCHA DETECTED!")
			
			# Try automatic solving first
			auto_solved = solve_captcha_automatically(page)
			
			if not auto_solved:
				# Fallback to manual solving
				print("    Please solve the CAPTCHA in the browser window...")
				print("    The script will wait for you to solve it...")
				
				# Wait for user to solve CAPTCHA (check every 5 seconds)
				while is_captcha_page(page):
					page.wait_for_timeout(5000)
				
				print("✅ CAPTCHA solved manually! Continuing...\n")
			
			# Give page time to load after CAPTCHA
			page.wait_for_timeout(2000)
			
		# Check if document is in force (pass href and title for tracking)
		if not is_document_in_force(page, href, title):
			return None, None

		# Extract title
		title_element = page.locator("h1.main-title").first
		title = title_element.inner_text() if title_element.count() > 0 else "Untitled Document"
		
		# Wait for content to confirm page load
		try:
			page.wait_for_selector("#docCont", timeout=5000)
		except:
			# If #docCont not found, check if it's maybe just a different structure or error
			if page.locator(".docContents").count() > 0:
				pass # Alternate class exists
			else:
				print("    Warning: Content element #docCont not found")
				return None, None
		
		# Extract the main content
		content_element = page.locator("#docCont")
		if content_element.count() == 0:
			# Try fallback to class
			content_element = page.locator(".docContents").first
		
		if content_element.count() == 0:
			print("    Warning: Content element not found")
			return None, None
		
		# Get the HTML content to preserve structure
		content_html = content_element.inner_html()
		
		return title, content_html
		
	except Exception as e:
		print(f"Error extracting document content: {e}")
		return None, None


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
		
		# Check main page and all frames
		for frame in page.frames:
			for indicator in datadome_indicators:
				try:
					if frame.locator(indicator).count() > 0:
						if not silent:
							print(f"    🔴 DataDome detected via: {indicator}")
						return frame
				except:
					continue
		
		return None
	except:
		return None


def solve_datadome_audio_captcha(page):
	"""Solve DataDome audio CAPTCHA by transcribing numbers"""
	print("\n🎧 Attempting to solve DataDome audio CAPTCHA...")
	
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
			print("    ⚠️  Timeout waiting for captcha elements")
			return False
		
		# Click on audio button to switch to audio mode
		audio_button = captcha_frame.locator("#captcha__audio__button")
		if audio_button.count() > 0:
			print("    Clicking audio button...")
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
			print("    ⚠️  Audio element not found")
			return False
		
		audio_url = audio_element.get_attribute("src")
		if not audio_url:
			print("    ⚠️  Audio URL not found")
			return False
		
		print(f"    📥 Downloading audio from: {audio_url[:50]}...")
		
		# Download the audio file
		try:
			response = requests.get(audio_url, timeout=30)
			if response.status_code != 200:
				print(f"    ⚠️  Failed to download audio: {response.status_code}")
				return False
			
			audio_data = response.content
		except Exception as e:
			print(f"    ⚠️  Error downloading audio: {e}")
			return False
		
		# Transcribe using AWS Transcribe
		numbers = transcribe_audio_captcha(audio_data)
		
		if not numbers or len(numbers) != 6:
			print(f"    ⚠️  Failed to get 6 digits, got: {numbers}")
			return False
		
		print(f"    🔢 Transcribed numbers: {numbers}")
		
		# Fill in the 6 input fields
		inputs = captcha_frame.locator(".audio-captcha-inputs").all()
		if len(inputs) != 6:
			print(f"    ⚠️  Expected 6 inputs, found {len(inputs)}")
			return False
		
		for i, digit in enumerate(numbers):
			inputs[i].fill(str(digit))
			page.wait_for_timeout(100)
		
		print("    ✅ Filled in all digits, submitting...")
		
		# Click verify button
		page.wait_for_timeout(500)
		verify_button = captcha_frame.locator(".audio-captcha-submit-button")
		if verify_button.count() > 0:
			verify_button.click()
			page.wait_for_timeout(3000)
		
		# Check if CAPTCHA was solved
		if not is_datadome_captcha(page):
			print("    ✅ DataDome CAPTCHA solved successfully!")
			return True
		else:
			print("    ❌ CAPTCHA still present, may need to retry...")
			return False
		
	except Exception as e:
		print(f"    ⚠️  Error solving DataDome CAPTCHA: {e}")
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
		
		print("    Title: Transcribing audio...")
		result = model.transcribe(temp_path)
		transcript = result["text"]
		
		# Extract only digits
		numbers = re.sub(r'[^0-9]', '', transcript)
		
		# Clean up
		if temp_path and os.path.exists(temp_path):
			os.unlink(temp_path)
			
		return numbers
			
	except Exception as e:
		print(f"    ⚠️  Transcription error: {e}")
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
			print("    ⚠️  AWS credentials not found for Bedrock")
			return None
		
		bedrock_client = boto3.client(
			"bedrock-runtime",
			region_name=BEDROCK_REGION,
			aws_access_key_id=aws_key,
			aws_secret_access_key=aws_secret,
		)
		return bedrock_client
	except Exception as e:
		print(f"    ⚠️  Failed to initialize Bedrock client: {e}")
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
		print(f"    ⚠️  Bedrock CAPTCHA solving failed: {e}")
		return ""


def solve_captcha_automatically(page):
	"""Attempt to automatically solve the CAPTCHA on the page"""
	print("\n🤖 Attempting automatic CAPTCHA solving...")
	
	# FIRST: Check for DataDome access restriction (not solvable - requires IP cooldown)
	if is_datadome_access_restricted(page):
		print("    🚫 DataDome ACCESS RESTRICTED - This is NOT a solvable CAPTCHA!")
		print("    🚫 IP has been rate-limited due to high download volume")
		wait_for_ip_cooldown(page, reason="DataDome Access Restricted - IP rate limited")
		# After cooldown, reload the page to check if restriction is lifted
		try:
			page.reload()
			page.wait_for_timeout(3000)
		except:
			pass
		# Check if still restricted
		if is_datadome_access_restricted(page):
			print("    ⚠️  Still restricted after cooldown - may need longer wait")
			return False
		return True
	
	# Check for DataDome CAPTCHA (slider/audio type) - solvable
	if is_datadome_captcha(page):
		print("    📌 Detected DataDome CAPTCHA (slider/audio type)")
		for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
			print(f"    DataDome attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")
			if solve_datadome_audio_captcha(page):
				return True
			# Reload captcha for next attempt
			try:
				reload_button = page.locator("#captcha__reload__button")
				if reload_button.count() > 0:
					reload_button.click()
					page.wait_for_timeout(2000)
			except:
				pass
		print("    ⚠️  DataDome auto-solve failed, waiting for manual input...")
		return False
	
	# Fall back to CanLII text CAPTCHA
	for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
		print(f"    Attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")
		
		try:
			# Wait for captcha image to load
			page.wait_for_selector("#captchaTag", timeout=5000)
			captcha_img = page.locator("#captchaTag")
			
			if captcha_img.count() == 0:
				print("    ⚠️  CAPTCHA image not found")
				continue
			
			# Take screenshot of the CAPTCHA image
			image_bytes = captcha_img.screenshot()
			
			if not image_bytes:
				print("    ⚠️  Failed to capture CAPTCHA image")
				continue
			
			# Solve using Bedrock
			captcha_solution = solve_captcha_with_bedrock(image_bytes)
			
			if not captcha_solution:
				print("    ⚠️  Could not extract CAPTCHA text")
				# Refresh captcha for next attempt by reloading
				page.reload()
				page.wait_for_timeout(2000)
				continue
			
			print(f"    🔍 Detected CAPTCHA text: {captcha_solution}")
			
			# Enter the solution
			captcha_input = page.locator("#captchaResponse")
			captcha_input.fill(captcha_solution)
			
			# Submit the form
			page.locator("input[type='submit'][value='ok']").click()
			page.wait_for_timeout(3000)
			
			# Check if CAPTCHA was solved successfully
			if not is_captcha_page(page):
				print("    ✅ CAPTCHA solved successfully!")
				return True
			else:
				print("    ❌ CAPTCHA solution was incorrect, retrying...")
				# Refresh page to get a new captcha challenge
				page.reload()
				page.wait_for_timeout(2000)
				
		except Exception as e:
			print(f"    ⚠️  Error during CAPTCHA solving: {e}")
			continue
	
	print("    ⚠️  Auto-solve failed after max attempts, waiting for manual input...")
	return False


def handle_captcha_interruption(page):
	"""
	Handle CAPTCHA detected during deep processing.
	Strategy: Check for access restriction -> Wait if needed -> Go to Homepage -> Solve -> Return True so caller can retry.
	"""
	print("\n🛑 CAPTCHA INTERRUPTION DETECTED!")
	print(f"   Initiating recovery protocol...")
	
	try:
		# Check for access restriction FIRST - requires waiting
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


def create_pdf_from_html(page, title, content_html, output_path):
	"""Generate a PDF from HTML content using Playwright"""
	try:
		# Create a complete HTML document with styling
		html_document = f"""
		<!DOCTYPE html>
		<html>
		<head>
			<meta charset="UTF-8">
			<title>{title}</title>
			<style>
				@page {{
					size: A4;
					margin: 2cm;
				}}
				body {{
					font-family: Arial, sans-serif;
					line-height: 1.6;
					color: #333;
					max-width: 210mm;
					margin: 0 auto;
					padding: 20px;
				}}
				h1 {{
					color: #1a1a1a;
					border-bottom: 2px solid #333;
					padding-bottom: 10px;
					margin-bottom: 20px;
				}}
				h2 {{
					color: #2a2a2a;
					margin-top: 25px;
					margin-bottom: 15px;
				}}
				h3 {{
					color: #3a3a3a;
					margin-top: 20px;
					margin-bottom: 10px;
				}}
				section {{
					margin-bottom: 20px;
				}}
				.order {{
					margin-left: 20px;
				}}
				p {{
					margin-bottom: 10px;
				}}
			</style>
		</head>
		<body>
			<h1>{title}</h1>
			{content_html}
		</body>
		</html>
		"""
		
		# Create a temporary HTML file
		temp_html_path = output_path.replace('.pdf', '_temp.html')
		with open(temp_html_path, 'w', encoding='utf-8') as f:
			f.write(html_document)
		
		# Navigate to the HTML file and generate PDF using Playwright
		page.goto(f"file:///{os.path.abspath(temp_html_path).replace(os.sep, '/')}", wait_until="load")
		page.pdf(path=output_path, format='A4', print_background=True)
		
		# Clean up temporary HTML file
		os.remove(temp_html_path)
		
		print(f"PDF created: {output_path}")
		return True
		
	except Exception as e:
		print(f"Error creating PDF: {e}")
		return False


def handle_cookie_consent(page):
	"""Handle cookie consent banner if it appears"""
	print("Checking for cookie banner...")
	
	try:
		# Check if banner exists first
		if page.locator("#cookieConsentBanner").count() > 0:
			print("Cookie banner detected")
			
			# Try JavaScript click first (most reliable)
			try:
				page.evaluate("""
					const btn = document.getElementById('understandCookieConsent');
					if (btn) btn.click();
				""")
				print("Cookie consent clicked successfully (JavaScript)")
				page.wait_for_timeout(1500)
			except Exception as e:
				print(f"JavaScript click failed: {e}")
				
				# Fallback to Playwright click
				try:
					page.click("#understandCookieConsent", timeout=3000)
					print("Cookie consent clicked successfully (Playwright)")
					page.wait_for_timeout(1500)
				except:
					print("Could not dismiss cookie banner")
			
			# Force remove the blocker if it still exists
			try:
				page.evaluate("""
					const blocker = document.getElementById('cookieConsentBlocker');
					if (blocker) blocker.remove();
					const container = document.getElementById('cookieConsentContainer');
					if (container) container.remove();
				""")
				print("Removed cookie consent blocker")
			except:
				pass
		else:
			print("No cookie banner found")
			
	except Exception as e:
		print(f"Cookie consent handling error: {e}")


def process_legislation_document(page, href, title, citation, prefix, tracking_data):
	"""Process a single legislation document (download, PDF, S3, track)"""
	# Create document key for tracking (use href only to avoid duplicates across categories)
	doc_key = href
	
	# Check local tracking
	if is_already_processed(tracking_data, doc_key):
		print(f"    ⏭️  Skipping (already processed)")
		return False

	# Create sanitized filename WITHOUT prefix - just citation and title
	if citation:
		safe_filename = sanitize_filename(f"{citation}_{title}"[:150])
	else:
		safe_filename = sanitize_filename(f"{title}"[:150])
	s3_key = f"{safe_filename}.pdf"
	

		
	# Go to document page
	doc_url = f"{BASE_URL}{href}"
	try:
		try:
			page.goto(doc_url, wait_until="load", timeout=30000)
		except Exception as e:
			print(f"    ⚠️  Navigation error: {e}")
			
		# Check for CAPTCHA interruption
		if is_captcha_page(page):
			print("    ⚠️  CAPTCHA detected on document page!")
			if handle_captcha_interruption(page):
				print("    🔄 Resuming document processing after recovery...")
				# Retry navigation
				page.goto(doc_url, wait_until="load")
			else:
				print("    ❌ Could not recover from CAPTCHA. Skipping this doc.")
				return False

		page.wait_for_load_state("domcontentloaded")
		page.wait_for_timeout(WAIT_MS)
		
		# Extract content (checks for in-force status inside)
		doc_title, content_html = extract_document_content(page, href, title)
		
		if doc_title and content_html:
			pdf_path = os.path.join(OUTPUT_DIR, s3_key)
			
			# Generate PDF
			if create_pdf_from_html(page, doc_title, content_html, pdf_path):
				# Upload to S3
				if upload_to_s3(pdf_path, s3_key):
					delete_local_file(pdf_path)
					mark_as_processed(tracking_data, {
						"key": doc_key,
						"title": title,
						"citation": citation,
						"href": href,
						"url": f"{BASE_URL}{href}",
						"s3_key": s3_key
					})
					delay_between_downloads()
					return True
	except Exception as e:
		print(f"    Error processing document {title}: {e}")
	
	return False


def extract_dropdown_items(page, row, main_title):
	"""Extract items from regulations/amendments dropdown if present"""
	dropdown_items = []
	
	try:
		# Check if row has a dropdown toggle (Regulations, Amendments, etc.)
		# The toggle is inside the second <td> within a <span class="text-end ps-2">
		dropdown_toggle = row.locator("a.pointer.text-nowrap").first
		
		if dropdown_toggle.count() == 0:
			return dropdown_items
		
		toggle_text = dropdown_toggle.inner_text().strip()
		print(f"    🔽 Found dropdown toggle: '{toggle_text}'")
		
		# Check if already expanded (angle-up icon means expanded, angle-down means collapsed)
		icon = dropdown_toggle.locator("i.fa")
		is_expanded = False
		if icon.count() > 0:
			icon_class = icon.get_attribute("class") or ""
			is_expanded = "fa-angle-up" in icon_class
			print(f"    📍 Dropdown state: {'Expanded' if is_expanded else 'Collapsed'} (icon: {icon_class})")
		
		# Click to expand if not already expanded
		if not is_expanded:
			try:
				print(f"    👆 Clicking to expand dropdown...")
				
				# Try JavaScript click first to bypass any blockers
				try:
					dropdown_toggle.evaluate("el => el.click()")
					print(f"    ✅ Clicked via JavaScript")
				except:
					# Fallback to regular click
					dropdown_toggle.click(timeout=3000)
					print(f"    ✅ Clicked via Playwright")
				
				page.wait_for_timeout(800)
			except Exception as e:
				print(f"    ⚠️  Could not click dropdown: {e}")
				# Try to remove cookie blocker and retry once
				try:
					page.evaluate("""
						const blocker = document.getElementById('cookieConsentBlocker');
						if (blocker) blocker.remove();
					""")
					print(f"    🔧 Removed cookie blocker, retrying...")
					dropdown_toggle.evaluate("el => el.click()")
					page.wait_for_timeout(800)
					print(f"    ✅ Clicked after blocker removal")
				except Exception as retry_error:
					print(f"    ❌ Retry failed: {retry_error}")
					return dropdown_items
		
		# Find the dropdown container (regulation_XXXXX or legislation_XXXXX)
		# It's inside the same <td> as a sibling div
		dropdown_div = row.locator("div[id^='regulation_']").first
		if dropdown_div.count() == 0:
			dropdown_div = row.locator("div[id^='legislation_']").first
		
		if dropdown_div.count() == 0:
			print(f"    ⚠️  No dropdown container found (div[id^='regulation_'] or div[id^='legislation_'])")
			return dropdown_items
		
		dropdown_id = dropdown_div.get_attribute("id")
		print(f"    📦 Found dropdown container: {dropdown_id}")
		
		# Wait for the dropdown to be visible
		page.wait_for_timeout(300)
		
		# Get all direct children (both <div> headers and <ul> lists are siblings)
		# Structure: <div id="regulation_XXX"><div class="pt-1">In force</div><ul>...</ul><div>Repealed...</div><ul>...</ul></div>
		all_children = dropdown_div.locator("> *").all()
		print(f"    📋 Found {len(all_children)} direct children in dropdown")
		
		current_section_type = ""
		
		for idx, child in enumerate(all_children):
			try:
				tag_name = child.evaluate("el => el.tagName").lower()
				print(f"    📦 Child {idx+1}: <{tag_name}>")
				
				if tag_name == "div":
					# This is likely a section header
					child_text = child.inner_text().strip()
					child_style = child.get_attribute("style") or ""
					child_class = child.get_attribute("class") or ""
					has_bold = "font-weight: bold" in child_style or "pt-1" in child_class
					
					print(f"      📄 Div text: '{child_text[:60]}', has_bold={has_bold}")
					
					if has_bold:
						# This is a section header
						print(f"      ▶️  Detected section header: '{child_text}'")
						# Check for "Repealed" FIRST to skip it
						if "repealed" in child_text.lower() or "spent" in child_text.lower() or "not in force" in child_text.lower():
							current_section_type = ""  # Skip these sections
							print(f"      ⏭️  Skipping section (repealed/spent/not in force)")
						elif "in force" in child_text.lower():
							current_section_type = "regulation"
							print(f"      ✅ Set section type to: regulation")
						elif "amended statutes" in child_text.lower():
							current_section_type = "amended_statute"
							print(f"      ✅ Set section type to: amended_statute")
						elif "amended regulations" in child_text.lower():
							current_section_type = "amended_regulation"
							print(f"      ✅ Set section type to: amended_regulation")
						else:
							current_section_type = ""  # Skip unknown sections
							print(f"      ⏭️  Skipping section (unknown type)")
				
				elif tag_name == "ul":
					# This is a list of items under the current section
					print(f"      📝 Processing <ul> under section type: '{current_section_type}'")
					
					# Skip if no valid section type
					if not current_section_type:
						print(f"      ⏭️  Skipping (no valid section type set)")
						continue
					
					# Extract all links from the list
					list_items = child.locator("li").all()
					print(f"      📊 Found {len(list_items)} list items")
					
					for li_idx, li in enumerate(list_items):
						link = li.locator("a").first
						
						if link.count() == 0:
							print(f"        ⚠️  Item {li_idx+1}: No link found")
							continue
						
						item_href = link.get_attribute("href")
						item_title = link.inner_text().strip()
						
						# Get citation (the <span class="nowrap"> next to the link)
						citation_span = li.locator("span.nowrap")
						item_citation = citation_span.inner_text().strip() if citation_span.count() > 0 else ""
						
						print(f"        ✅ Item {li_idx+1}: '{item_title[:40]}...' [{item_citation}] -> {item_href}")
						
						if item_href:
							dropdown_items.append({
								"href": item_href,
								"title": item_title,
								"citation": item_citation,
								"parent_title": main_title,
								"type": current_section_type
							})
			except Exception as e:
				print(f"    ⚠️  Warning processing child {idx+1}: {e}")
				continue
		
		if len(dropdown_items) > 0:
			print(f"    ✅ Total extracted: {len(dropdown_items)} nested items")
		else:
			print(f"    ⚠️  No nested items extracted from dropdown")
		return dropdown_items
		
	except Exception as e:
		print(f"    ❌ Error extracting dropdown items: {e}")
		import traceback
		traceback.print_exc()
		return dropdown_items


def process_category_page(page, tracking_data, category_url):
	"""Process all items in a category page in real-time"""
	try:
		# Wait for the table to be populated
		page.wait_for_selector("#legislationsContainer tr", timeout=10000)
		
		# Click "Show more results" until all items are loaded
		print("  Checking for 'Show more results' button...")
		while True:
			try:
				show_more_button = page.locator("span.showMoreResults")
				if show_more_button.count() > 0 and show_more_button.is_visible():
					print("  Clicking 'Show more results'...")
					show_more_button.click()
					page.wait_for_timeout(1500)
					
					# Quick check for CAPTCHA during pagination
					if is_captcha_page(page):
						print("⚠️ CAPTCHA detected during pagination!")
						if handle_captcha_interruption(page):
							print("    🔄 Resuming pagination after recovery...")
							page.goto(category_url, wait_until="load")
						else:
							print("    Waiting for manual CAPTCHA solve...")
							while is_captcha_page(page):
								page.wait_for_timeout(5000)
						page.wait_for_timeout(1500)
				else:
					break
			except Exception:
				break
		
		# IMPORTANT: Collect ALL item data FIRST before navigating away
		# This prevents stale element references when we navigate to document pages
		items_to_process = []
		rows = page.locator("#legislationsContainer tr").all()
		total_rows = len(rows)
		print(f"Found {total_rows} legislation items to process")
		
		for row in rows:
			try:
				# Extract main link info
				link_element = row.locator("a.canlii").first
				if link_element.count() == 0:
					continue
					
				href = link_element.get_attribute("href")
				title = link_element.inner_text().strip()
				
				# Handle different table structures:
				# - Statutes/AStatutes: <td class="decisionDate">Citation</td>
				# - Regulations: <td><a>Title</a>, <span class="nowrap">Citation</span></td>
				citation_element = row.locator("td.decisionDate")
				if citation_element.count() > 0:
					citation = citation_element.inner_text().strip()
				else:
					# Try to get citation from span.nowrap in the first cell
					nowrap_element = row.locator("td").first.locator("span.nowrap").first
					if nowrap_element.count() > 0:
						citation = nowrap_element.inner_text().strip()
					else:
						citation = ""
				
				# Add main item
				items_to_process.append({
					"href": href,
					"title": title,
					"citation": citation,
					"parent_title": None,
					"type": "main"
				})
				
				# Extract dropdown items (regulations, amendments, etc.)
				dropdown_items = extract_dropdown_items(page, row, title)
				items_to_process.extend(dropdown_items)
				
			except Exception as e:
				print(f"  Error extracting row data: {e}")
				continue
		
		print(f"  Collected {len(items_to_process)} items data from category page (includes main + nested)")
		
		processed_count = 0
		
		# Now process each item - we have all the data we need stored
		for i, item in enumerate(items_to_process, 1):
			try:
				href = item["href"]
				title = item["title"]
				citation = item["citation"]
				item_type = item.get("type", "main")
				parent_title = item.get("parent_title")
				
				# Create descriptive label
				if parent_title:
					type_label = item_type.replace("_", " ").title()
					print(f"\n  Processing item {i}/{len(items_to_process)}: {title} [{type_label} of '{parent_title}']")
				else:
					print(f"\n  Processing item {i}/{len(items_to_process)}: {title}")
				
				# Determine prefix based on category URL
				if "/const" in category_url:
					prefix = "const"
					if item_type != "main":
						prefix = f"const_{item_type}"
				elif "/stat" in category_url:
					prefix = "stat"
					if item_type != "main":
						prefix = f"stat_{item_type}"
				elif "/astat" in category_url:
					prefix = "astat"
					if item_type != "main":
						prefix = f"astat_{item_type}"
				elif "/regu" in category_url:
					prefix = "regu"
					if item_type != "main":
						prefix = f"regu_{item_type}"
				else:
					prefix = "main"
					if item_type != "main":
						prefix = f"main_{item_type}"
				
				# Process Document
				if process_legislation_document(page, href, title, citation, prefix, tracking_data):
					processed_count += 1
				else:
					print(f"    ⚠️  Skipped or failed")
				
				# After processing, navigate back to the category page
				# This ensures we can continue processing from a known state
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
				page.wait_for_timeout(1000)
				
			except Exception as e:
				print(f"  Error processing item {i}: {e}")
				# Try to recover by navigating back to category page
				try:
					page.goto(category_url, wait_until="load")
					page.wait_for_load_state("networkidle")
					page.wait_for_timeout(1000)
				except:
					pass
				continue
		
		return processed_count

	except Exception as e:
		print(f"Error processing category page: {e}")
		return 0


def main():
	# Create output directory
	os.makedirs(OUTPUT_DIR, exist_ok=True)
	
	# Load tracking data for resume functionality
	tracking_data = load_tracking_data()
	print(f"Loaded tracking data: {len(tracking_data.get('processed_documents', []))} documents already processed")
	
	with sync_playwright() as p:
		# Determine headless mode:
		# - Default to HEADLESS=False on Windows (for debug)
		# - Default to HEADLESS=True on Linux (for server)
		# - Allow override via env var
		system_os = platform.system()
		env_headless = os.getenv("HEADLESS")
		
		if env_headless is not None:
			is_headless = env_headless.lower() == "true"
		else:
			is_headless = system_os == "Linux"
			
		print(f"Running on {system_os}, Headless: {is_headless}")

		browser = p.chromium.launch(
			headless=is_headless,
			channel="chrome",  # Use actual Chrome if available
			args=get_browser_args()
		)
		
		context = browser.new_context(
			viewport={"width": 1920, "height": 1080},
			user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
			locale="en-US",
			timezone_id="America/Toronto",
			permissions=["geolocation"],
			geolocation={"latitude": 45.4215, "longitude": -75.6972} # Ottawa
		)
		
		# Inject all stealth scripts
		for script in get_stealth_scripts():
			context.add_init_script(script)

		page = context.new_page()
		
		# Add random mouse movement to simulate human behavior
		page.mouse.move(random.randint(100, 500), random.randint(100, 500))
		
		page.goto(START_URL, wait_until="load")
		try:
			page.wait_for_load_state("load", timeout=30000)
		except:
			print("Warning: Initial load timeout, proceeding...")
		page.wait_for_timeout(WAIT_MS)
		
		# Check for CAPTCHA FIRST (DataDome appears before cookie consent)
		print("\n🔍 Checking for CAPTCHA on initial page...")
		datadome_detected = is_datadome_captcha(page)
		canlii_detected = page.locator("#captchaTag").count() > 0
		print(f"    DataDome CAPTCHA: {'DETECTED' if datadome_detected else 'not found'}")
		print(f"    CanLII CAPTCHA: {'DETECTED' if canlii_detected else 'not found'}")
		
		if datadome_detected or canlii_detected or is_captcha_page(page):
			print("\n⚠️  CAPTCHA detected on initial page!")
			auto_solved = solve_captcha_automatically(page)
			if not auto_solved:
				print("    Please solve the CAPTCHA in the browser window...")
				while is_captcha_page(page):
					page.wait_for_timeout(5000)
				print("✅ CAPTCHA solved! Continuing...")
			page.wait_for_timeout(3000)
			# Wait briefly to see if page auto-reloads (DataDome often reloads automatically)
			print("    Waiting for page to stabilize...")
			page.wait_for_timeout(5000)
			
			# Check if we are already on the page with content
			if page.locator("h2", has_text=SECTION_TITLE).count() > 0:
				print("    Page content appears loaded, skipping reload.")
			else:
				print("    Reloading page explicitly...")
				try:
					# Use commit to just start navigation, then wait for load
					# This prevents hanging if the load event is delayed or complex
					page.goto(START_URL, wait_until="commit", timeout=60000)
					try:
						page.wait_for_load_state("domcontentloaded", timeout=60000)
					except:
						pass
				except Exception as e:
					print(f"Warning: Navigation timeout after CAPTCHA, continuing anyway: {e}")
				
			page.wait_for_timeout(WAIT_MS)
		
		# Handle cookie consent (only after CAPTCHA is solved)
		handle_cookie_consent(page)
		
		try:
			page.wait_for_load_state("load", timeout=10000)
		except:
			pass
		page.wait_for_timeout(WAIT_MS)
		
		# Step 1: Collect category links
		print("\n=== Collecting legislation category links ===")
		category_links = collect_category_links(page, SECTION_TITLE)
		print(f"Found {len(category_links)} category links")
		
		# If still no categories found, check for CAPTCHA again
		if len(category_links) == 0:
			print("⚠️  No categories found, checking for CAPTCHA...")
			if is_captcha_page(page):
				auto_solved = solve_captcha_automatically(page)
				if not auto_solved:
					print("    Please solve the CAPTCHA in the browser window...")
					while is_captcha_page(page):
						page.wait_for_timeout(5000)
					print("✅ CAPTCHA solved! Continuing...")
				page.wait_for_timeout(3000)
				# Wait and Reload
				print("    Waiting for page to stabilize...")
				page.wait_for_timeout(5000)
				
				if page.locator("h2", has_text=SECTION_TITLE).count() > 0:
					print("    Page content appears loaded, skipping reload.")
				else:
					print("    Reloading page explicitly...")
					try:
						page.goto(START_URL, wait_until="commit", timeout=60000)
						try:
							page.wait_for_load_state("domcontentloaded", timeout=60000)
						except:
							pass
					except:
						print("Warning: Navigation timeout, continuing...")
				
				page.wait_for_timeout(WAIT_MS)
				category_links = collect_category_links(page, SECTION_TITLE)
				print(f"Found {len(category_links)} category links after CAPTCHA")
		
		total_processed = 0
		
		# Step 2: Live Processing per category
		for i, category_href in enumerate(category_links, 1):
			print(f"\n=== Processing category {i}/{len(category_links)}: {category_href} ===")
			
			category_url = f"{BASE_URL}{category_href}"
			try:
				page.goto(category_url, wait_until="domcontentloaded", timeout=60000)
			except:
				print("Warning: Category page navigation timeout, continuing...")
			try:
				page.wait_for_load_state("domcontentloaded", timeout=15000)
			except:
				pass
			page.wait_for_timeout(WAIT_MS)
			
			# Check for CAPTCHA when entering category page
			if is_captcha_page(page):
				print("⚠️  CAPTCHA detected on category page!")
				auto_solved = solve_captcha_automatically(page)
				if not auto_solved:
					print("    Please solve the CAPTCHA in the browser window...")
					while is_captcha_page(page):
						page.wait_for_timeout(5000)
					print("✅ CAPTCHA solved! Continuing...")
				page.wait_for_timeout(3000)
				# Reload the category page
				try:
					page.goto(category_url, wait_until="domcontentloaded", timeout=60000)
				except:
					pass
				try:
					page.wait_for_load_state("domcontentloaded", timeout=15000)
				except:
					pass
				page.wait_for_timeout(WAIT_MS)
			
			# Process all items in this category immediately
			count = process_category_page(page, tracking_data, category_url)
			total_processed += count
		
		print(f"\n=== Scraping Complete ===")
		print(f"Total documents downloaded: {total_processed}")
		print(f"PDFs saved in S3: s3://{S3_BUCKET_NAME}/")
		
		browser.close()


if __name__ == "__main__":
	main()
