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
import logging
import sys

# Load environment variables
load_dotenv()

# Configure logging with both console and file handlers
# Create logs directory if it doesn't exist
os.makedirs('logs', exist_ok=True)

# Create formatters
log_format = '%(asctime)s - %(levelname)s - %(message)s'
date_format = '%Y-%m-%d %H:%M:%S'
formatter = logging.Formatter(log_format, datefmt=date_format)

# Setup logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Console handler with forced flush for real-time output
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
# Force flush after each log message for real-time output
class FlushStreamHandler(logging.StreamHandler):
	def emit(self, record):
		super().emit(record)
		self.flush()
console_handler = FlushStreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# File handler for persistent logs
log_filename = f"logs/crawler_{time.strftime('%Y%m%d_%H%M%S')}.log"
file_handler = logging.FileHandler(log_filename, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

logger.info(f"📋 Logging initialized - Console and file: {log_filename}")

# Prevent duplicate logs from propagating to root logger
logger.propagate = False

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
ACCESS_RESTRICTED_WAIT_MIN = 25  # Minimum wait time in minutes (base time)
ACCESS_RESTRICTED_WAIT_MAX = 35  # Maximum wait time in minutes (base time)
# For consecutive cooldowns, wait time is multiplied by attempt number
# Example: attempt=1: 25-35min, attempt=2: 50-70min, attempt=3: 75-105min



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


def wait_for_ip_cooldown(page, reason="access restriction", attempt=1):
	"""Wait for IP restriction to clear with exponential backoff
	
	Args:
		page: Playwright page object
		reason: Reason for the cooldown (for logging)
		attempt: Cooldown attempt number (1, 2, 3...) - multiplies wait time
	"""
	# Calculate wait time with exponential backoff
	base_wait_minutes = random.randint(ACCESS_RESTRICTED_WAIT_MIN, ACCESS_RESTRICTED_WAIT_MAX)
	wait_minutes = base_wait_minutes * attempt  # Multiply by attempt number
	wait_seconds = wait_minutes * 60
	
	logger.warning("\n" + "="*60)
	logger.warning("🚫 ACCESS RESTRICTED - IP COOLDOWN REQUIRED")
	logger.warning("="*60)
	logger.warning(f"Reason: {reason}")
	if attempt > 1:
		logger.warning(f"Consecutive cooldown attempt #{attempt} - Using {attempt}x wait time")
	logger.info(f"Waiting for {wait_minutes} minutes to let IP restriction clear...")
	logger.info(f"Start time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
	logger.info(f"Resume time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + wait_seconds))}")
	logger.warning("="*60 + "\n")
	
	# Use time.sleep instead of page.wait_for_timeout to avoid browser timeout
	for remaining_minutes in range(wait_minutes, 0, -1):
		print(f"    ⏳ {remaining_minutes} minute(s) remaining...")
		time.sleep(60)  # Sleep for 1 minute
	
	logger.info("\n" + "="*60)
	logger.info("✅ IP COOLDOWN COMPLETE - Resuming operations")
	logger.info("="*60 + "\n")
	
	return True


def get_cookies_dict(page):
	"""Get cookies from Playwright context as a dictionary"""
	cookies = page.context.cookies()
	cookie_dict = {}
	for cookie in cookies:
		cookie_dict[cookie['name']] = cookie['value']
	return cookie_dict


def get_firefox_launch_args():
	"""Get robust Firefox arguments for evasion"""
	return []


def get_firefox_user_prefs():
	"""Get Firefox user preferences for stealth"""
	return {
		"dom.webdriver.enabled": False,
		"useSystemGlobalMediaControls": False,
		# "marionette.enabled": False, # Commented out to prevent connection issues
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


def delete_from_s3(s3_key):
	"""Delete a file from S3 bucket"""
	try:
		# First check if file exists
		if not file_exists_in_s3(s3_key):
			logger.info(f"ℹ️  Not in S3 (already deleted or never uploaded): {s3_key}")
			return True
		
		s3_client = boto3.client(
			's3',
			aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
			aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
			region_name=os.getenv('AWS_REGION', 'us-east-1')
		)
		
		# Delete the object
		s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
		logger.info(f"🗑️  Deleted from S3: s3://{S3_BUCKET_NAME}/{s3_key}")
		return True
	except Exception as e:
		logger.error(f"✗ S3 deletion failed: {e}")
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
			logger.warning(f"⚠️  Could not load tracking file: {e}")
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


def remove_from_processed(tracking_data, document_key):
	"""Remove a document from tracking data (used when document is repealed)"""
	removed = False
	
	# Remove from processed_keys (old format)
	if "processed_keys" in tracking_data and document_key in tracking_data["processed_keys"]:
		tracking_data["processed_keys"].remove(document_key)
		removed = True
	
	# Remove from processed_documents (new format)
	if "processed_documents" in tracking_data:
		original_count = len(tracking_data["processed_documents"])
		tracking_data["processed_documents"] = [
			doc for doc in tracking_data["processed_documents"]
			if isinstance(doc, dict) and doc.get("key") != document_key
		]
		if len(tracking_data["processed_documents"]) < original_count:
			removed = True
	
	if removed:
		save_tracking_data(tracking_data)
		return True
	return False


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
		# Remove cookie modal first to prevent interference
		force_remove_cookie_modal(page)
		
		# Check for CAPTCHA first
		if is_captcha_page(page):
			logger.warning("\n⚠️  CAPTCHA DETECTED during content extraction!")
			
			if handle_captcha_interruption(page):
				logger.info("   🔄 Recovery successful, reloading document...")
				# Re-navigate to the document URL
				try:
					doc_url = f"{BASE_URL}{href}"
					page.goto(doc_url, wait_until="load")
					page.wait_for_load_state("domcontentloaded")
					force_remove_cookie_modal(page)
				except Exception as nav_e:
					logger.error(f"   ❌ Failed to reload document after recovery: {nav_e}")
					return None, None
			else:
				logger.error("   ❌ Failed to recover from CAPTCHA. Skipping document.")
				return None, None
			
			# Gives page time to load after recovery
			page.wait_for_timeout(2000)
			
		# Check if document is in force (pass href and title for tracking)
		if not is_document_in_force(page, href, title):
			return None, None

		# Extract title - try multiple selectors
		title = None
		title_selectors = [
			"h1.main-title",
			"h2.Title-of-Act",
			"section.intro h2",
			"h1"
		]
		for selector in title_selectors:
			title_element = page.locator(selector).first
			if title_element.count() > 0:
				title = title_element.inner_text().strip()
				break
		
		if not title:
			title = "Untitled Document"
		
		# Wait for content to confirm page load - try multiple selectors
		content_found = False
		content_selectors = ["#docCont", ".docContents", "div.docContents"]
		
		for selector in content_selectors:
			try:
				page.wait_for_selector(selector, timeout=5000)
				content_found = True
				break
			except:
				continue
		
		if not content_found:
			print("    Warning: Content element not found")
			return None, None
		
		# Extract the main content - try multiple selectors
		content_element = None
		for selector in content_selectors:
			content_element = page.locator(selector).first
			if content_element.count() > 0:
				break
		
		if not content_element or content_element.count() == 0:
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
		# Primary CAPTCHA-specific indicators (these are definitive)
		primary_indicators = [
			"#captcha-container",
			"#ddv1-captcha-container",
			"#captcha__frame",
			"#captcha__audio__button",
			".captcha__human",
			".captcha__human__title",
			"[data-dd-captcha-container]",
			".sliderContainer",
		]
		
		# Check main page and all frames for primary indicators
		for frame in page.frames:
			for indicator in primary_indicators:
				try:
					if frame.locator(indicator).count() > 0:
						if not silent:
							logger.info(f"🔴 DataDome detected via: {indicator}")
						return frame
				except:
					continue
		
		# Secondary check: Only look for text indicators if they appear with CAPTCHA context
		# Check for "Verification Required" only if it's in a modal/overlay/captcha-like container
		try:
			for frame in page.frames:
				# Check if "Verification Required" exists
				if frame.locator("text=Verification Required").count() > 0:
					# Verify it's in a CAPTCHA context by checking for captcha-related parent elements
					verification_elements = frame.locator("text=Verification Required").all()
					for elem in verification_elements:
						try:
							# Check if this element is inside a captcha-related container
							parent_html = elem.evaluate("el => el.closest('div')?.outerHTML || ''")
							if any(keyword in parent_html.lower() for keyword in ['captcha', 'datadome', 'challenge', 'modal', 'overlay']):
								if not silent:
									logger.info(f"🔴 DataDome detected via: text=Verification Required (in CAPTCHA context)")
								return frame
						except:
							pass
				
				# Check for slider challenge text (this is more specific)
				if frame.locator("text=Slide right to secure your access").count() > 0:
					if not silent:
						logger.info(f"🔴 DataDome detected via: text=Slide right to secure your access")
					return frame
		except:
			pass
		
		return None
	except:
		return None


def is_datadome_access_restricted(page):
	"""
	Check if the DataDome CAPTCHA is showing 'Access is temporarily restricted' message.
	This variant has no solvable CAPTCHA - requires waiting for IP cooldown.
	"""
	try:
		# Check all frames for the access restricted message
		for frame in page.frames:
			try:
				# Check for the specific title element with access restricted text
				title_element = frame.locator(".captcha__human__title")
				if title_element.count() > 0:
					title_text = title_element.inner_text().lower().strip()
					if "temporarily restricted" in title_text or "access" in title_text and "restricted" in title_text:
						print(f"    🚫 DataDome ACCESS RESTRICTED detected: '{title_text}'")
						return True
				
				# Also check for the warning text about unusual activity
				warning_element = frame.locator(".captcha__robot__warning__why")
				if warning_element.count() > 0:
					warning_text = warning_element.inner_text().lower().strip()
					if "unusual activity" in warning_text or "detected" in warning_text:
						# This is an access restriction, not a solvable CAPTCHA
						# Check if there's NO audio button (meaning it's just a block page)
						audio_btn = frame.locator("#captcha__audio__button")
						slider = frame.locator(".sliderContainer, #captcha__slider")
						if audio_btn.count() == 0 and slider.count() == 0:
							print(f"    🚫 DataDome ACCESS RESTRICTED (no solvable elements): '{warning_text[:50]}...'")
							return True
			except:
				continue
		
		return False
	except:
		return False


def solve_datadome_audio_captcha(page):
	"""Solve DataDome audio CAPTCHA by transcribing numbers
	
	Returns:
		True if solved successfully
		False if failed but can retry
		None if timeout (indicates possible access restriction)
	"""
	logger.info("\n🎧 Attempting to solve DataDome audio CAPTCHA...")
	
	try:
		# Handle cookie banner first
		handle_cookie_consent(page)
		
		# Find the frame containing the CAPTCHA (silent to avoid repeated logging)
		captcha_frame = is_datadome_captcha(page, silent=True)
		if not captcha_frame:
			# Fallback to main page if not found (though it should be)
			captcha_frame = page
			
		# Wait for the captcha container to load
		try:
			captcha_frame.wait_for_selector("#captcha-container, .captcha-container, #captcha__audio__button", timeout=10000)
		except:
			logger.warning("⚠️  Timeout waiting for captcha elements")
			return None  # Signal timeout to caller
		
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
		
		# Transcribe using Whisper
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
			logger.warning("❌ CAPTCHA still present, refreshing page...")
			# Refresh page to get new CAPTCHA
			try:
				page.reload(wait_until="domcontentloaded")
				page.wait_for_timeout(2000)
			except:
				pass
			return False
		
	except Exception as e:
		logger.error(f"⚠️  Error solving DataDome CAPTCHA: {e}")
		# Refresh page on error
		try:
			page.reload(wait_until="domcontentloaded")
			page.wait_for_timeout(2000)
		except:
			pass
		return False


def force_remove_cookie_modal(page):
	"""Aggressively remove cookie consent modal and backdrop"""
	try:
		page.evaluate("""
			// Remove by ID
			const idsToRemove = [
				'cookieConsentBlocker',
				'cookieConsentBanner',
				'cookieConsentModal',
				'cookieConsentContainer'
			];
			idsToRemove.forEach(id => {
				const el = document.getElementById(id);
				if (el) {
					el.style.display = 'none';
					el.remove();
				}
			});
			
			// Remove modal backdrops
			const backdrops = document.querySelectorAll('.modal-backdrop, [class*="modal"]');
			backdrops.forEach(el => {
				if (el.id !== 'main' && !el.querySelector('#docCont')) {
					el.style.display = 'none';
					el.remove();
				}
			});
			
			// Reset body styles
			document.body.classList.remove('modal-open');
			document.body.style.overflow = 'auto';
			document.body.style.paddingRight = '0';
			
			// Remove any overlays
			const overlays = document.querySelectorAll('[style*="z-index"][style*="fixed"], [style*="z-index"][style*="absolute"]');
			overlays.forEach(el => {
				if (el.id.includes('cookie') || el.className.includes('cookie') || el.className.includes('modal')) {
					el.style.display = 'none';
					el.remove();
				}
			});
		""")
		page.wait_for_timeout(200)
	except:
		pass


def solve_canlii_audio_captcha(page):
	"""Solve CanLII standard audio CAPTCHA"""
	logger.info("\n🎧 Attempting to solve CanLII audio CAPTCHA...")
	
	# Handle cookie banner and blocking elements
	handle_cookie_consent(page)
	force_remove_cookie_modal(page)
	
	try:
		# Check if audio is already visible
		audio_tag = page.locator("#audioCaptchaTag")
		needs_toggle = True
		
		# Check visibility properly
		if audio_tag.count() > 0:
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
			logger.warning("❌ CAPTCHA solution incorrect, refreshing...")
			try:
				page.reload(wait_until="domcontentloaded")
				page.wait_for_timeout(2000)
			except:
				pass
			return False
			
	except Exception as e:
		logger.error(f"⚠️  Error solving CanLII audio CAPTCHA: {e}")
		try:
			page.reload(wait_until="domcontentloaded")
			page.wait_for_timeout(2000)
		except:
			pass
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
	logger.info("\n🤖 Attempting automatic CAPTCHA solving...")

	# Remove cookie consent blocker if present
	force_remove_cookie_modal(page)
	
	# First, check for DataDome CAPTCHA (slider/audio type)
	if is_datadome_captcha(page):
		# CRITICAL: Check if this is an "Access Restricted" variant (no solvable CAPTCHA)
		if is_datadome_access_restricted(page):
			logger.warning("🚫 This is a DataDome ACCESS RESTRICTED page - NOT a solvable CAPTCHA!")
			logger.warning("🚫 IP has been rate-limited. Triggering cooldown...")
			wait_for_ip_cooldown(page, reason="DataDome Access Restricted - IP rate-limited after high download volume", attempt=1)
			# After cooldown, check if access is restored
			page.goto(START_URL, wait_until="commit")
			page.wait_for_load_state("domcontentloaded")
			page.wait_for_timeout(3000)
			# Check again - might need another cooldown or regular CAPTCHA
			if is_datadome_access_restricted(page):
				logger.warning("⚠️  Still access restricted after cooldown, using 2x wait time...")
				wait_for_ip_cooldown(page, reason="Still access restricted after first cooldown", attempt=2)
				page.goto(START_URL, wait_until="commit")
				page.wait_for_load_state("domcontentloaded")
				page.wait_for_timeout(3000)
				# Check third time
				if is_datadome_access_restricted(page):
					logger.error("❌ Still restricted after 2 cooldowns. IP may be banned for extended period.")
					logger.error("Waiting one more time with 3x multiplier...")
					wait_for_ip_cooldown(page, reason="Third consecutive cooldown - possible long-term ban", attempt=3)
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
		consecutive_timeouts = 0
		for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
			logger.info(f"DataDome attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")
			solve_result = solve_datadome_audio_captcha(page)
			if solve_result is True:
				return True
			elif solve_result is None:  # Timeout detected
				consecutive_timeouts += 1
				# After 5 consecutive timeouts, assume it's actually an access restricted variant
				if consecutive_timeouts >= 5:
					logger.warning("🚫 Too many consecutive timeouts - checking if this is actually Access Restricted...")
					if is_datadome_access_restricted(page):
						logger.warning("🚫 Confirmed: This is Access Restricted, not a solvable CAPTCHA!")
						wait_for_ip_cooldown(page, reason="DataDome Access Restricted detected after timeout pattern", attempt=1)
						page.goto(START_URL, wait_until="commit")
						page.wait_for_load_state("domcontentloaded")
						return solve_captcha_automatically(page)
					else:
						logger.warning("⚠️  Elements not loading, but not access restricted. May need manual solve.")
						break
			else:
				consecutive_timeouts = 0  # Reset counter on non-timeout failures
			
			# Check if it became access restricted during attempts
			if is_datadome_access_restricted(page):
				logger.warning("🚫 CAPTCHA attempts triggered access restriction!")
				wait_for_ip_cooldown(page, reason="Access restricted after CAPTCHA solve attempts", attempt=1)
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
			wait_for_ip_cooldown(page, reason="DataDome Access Restricted during scraping", attempt=1)
			
			# After waiting, go to homepage and check again
			logger.info(f"Navigating to homepage ({START_URL}) after cooldown...")
			page.goto(START_URL, wait_until="commit")
			page.wait_for_load_state("domcontentloaded")
			
			# If still restricted after waiting, wait again with 2x multiplier
			if is_datadome_access_restricted(page):
				logger.warning("⚠️  Still access restricted after first cooldown, using 2x wait time...")
				wait_for_ip_cooldown(page, reason="DataDome still restricted after first cooldown", attempt=2)
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
			wait_for_ip_cooldown(page, reason="Access restriction detected during scraping", attempt=1)
			
			# After waiting, go to homepage and check again
			print(f"   Navigating to homepage ({START_URL}) after cooldown...")
			page.goto(START_URL, wait_until="commit")
			page.wait_for_load_state("domcontentloaded")
			
			# If still restricted after waiting, wait again with 2x multiplier
			if is_access_restricted_page(page):
				print("   ⚠️  Still restricted after first cooldown, using 2x wait time...")
				wait_for_ip_cooldown(page, reason="Access still restricted after first cooldown", attempt=2)
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
			wait_for_ip_cooldown(page, reason="Access restriction on homepage", attempt=1)
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



def create_pdf_from_html(chrome_page, title, content_html, output_path):
	"""Generate a PDF from HTML content using Chrome/Playwright"""
	try:
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
				h1, h2.Title-of-Act {{
					color: #1a1a1a;
					border-bottom: 2px solid #333;
					padding-bottom: 10px;
					margin-bottom: 20px;
					font-size: 1.8em;
				}}
				h2.Part, h3.Subheading, h4.Subheading {{
					color: #2a2a2a;
					margin-top: 25px;
					margin-bottom: 15px;
					font-weight: bold;
				}}
				h2.Part {{
					font-size: 1.5em;
					border-bottom: 1px solid #666;
				}}
				h3.Subheading {{
					font-size: 1.3em;
				}}
				h4.Subheading {{
					font-size: 1.1em;
				}}
				.MarginalNote {{
					font-style: italic;
					color: #666;
					margin: 10px 0 5px 0;
					font-size: 0.9em;
				}}
				.Section, .Subsection {{
					margin: 12px 0;
					line-height: 1.8;
				}}
				.Section strong, .Subsection strong {{
					margin-right: 8px;
				}}
				.sectionLabel {{
					font-weight: bold;
					color: #000;
				}}
				p.centered {{
					text-align: center;
					margin: 15px 0;
				}}
				p.right-align {{
					text-align: right;
					margin: 10px 0;
				}}
				p.indent-0-0, p.indent-1-0 {{
					margin: 8px 0;
				}}
				p.indent-1-0 {{
					margin-left: 20px;
				}}
				ul.ProvisionList {{
					list-style-type: none;
					padding-left: 0;
					margin: 15px 0;
				}}
				/* Schedule heading styles */
				.Schedule header {{
					margin: 30px 0 20px 0;
				}}
				h2.scheduleLabel {{
					font-size: 1.5em;
					font-weight: bold;
					color: #1a1a1a;
					margin: 0;
					padding: 0;
					border: none;
				}}
				.scheduleLabel {{
					display: block;
					font-weight: bold;
					margin-bottom: 5px;
				}}
				.scheduleTitleText {{
					display: block;
					font-weight: normal;
					font-size: 0.85em;
					margin-top: 5px;
				}}
				/* Other document elements */
				.ChapterNumber, .EnablingAct, .LongTitle {{
					margin: 8px 0;
					font-weight: normal;
				}}
				.ChapterNumber {{
					font-style: italic;
				}}
				.EnablingAct {{
					font-weight: bold;
					text-transform: uppercase;
				}}
				.FlushLeft {{
					margin: 5px 0;
				}}
				ul.ProvisionList > li {{
					margin: 12px 0;
				}}
				.listItemBlock1, .listItemBlock3 {{
					display: flex;
					margin: 10px 0;
				}}
				.listItemLabel {{
					font-weight: bold;
					min-width: 40px;
					flex-shrink: 0;
				}}
				.listItemText1, .listItemText2 {{
					flex: 1;
				}}
				.Smallcaps {{
					font-variant: small-caps;
				}}
				.Repealed {{
					color: #999;
					font-style: italic;
				}}
				.order {{
					margin: 20px 0;
				}}
				.intro {{
					margin-bottom: 25px;
				}}
				section {{
					margin: 20px 0;
				}}
				/* Hide interactive elements */
				.bootstrap, .viibes-marker-toolbox, .viibes-marker {{
					display: none !important;
				}}
				/* Clean up links */
				a {{
					color: #0066cc;
					text-decoration: none;
				}}
				sup {{
					font-size: 0.7em;
				}}
				table {{
					border-collapse: collapse;
					width: 100%;
					margin: 15px 0;
				}}
				table td, table th {{
					padding: 8px;
					border: 1px solid #ddd;
				}}
			</style>
		</head>
		<body>
			<h1>{title}</h1>
			{content_html}
		</body>
		</html>
		"""
		
		temp_html_path = output_path.replace('.pdf', '_temp.html')
		with open(temp_html_path, 'w', encoding='utf-8') as f:
			f.write(html_document)
		
		chrome_page.goto(f"file:///{os.path.abspath(temp_html_path).replace(os.sep, '/')}", wait_until="load")
		chrome_page.pdf(path=output_path, format='A4', print_background=True)
		
		os.remove(temp_html_path)
		
		print(f"  ✓ PDF created: {os.path.basename(output_path)}")
		return True
		
	except Exception as e:
		print(f"  ✗ Error creating PDF: {e}")
		return False


def handle_cookie_consent(page):
	"""Handle cookie consent banner if it appears"""
	try:
		# First, aggressively remove with JavaScript (most reliable)
		page.evaluate("""
			const banner = document.getElementById('cookieConsentBanner');
			if (banner) {
				banner.style.display = 'none';
				banner.remove();
			}
			const blocker = document.getElementById('cookieConsentBlocker');
			if (blocker) {
				blocker.style.display = 'none';
				blocker.remove();
			}
			const modal = document.getElementById('cookieConsentModal');
			if (modal) {
				modal.style.display = 'none';
				modal.remove();
			}
			const backdrops = document.querySelectorAll('.modal-backdrop');
			backdrops.forEach(el => el.remove());
			document.body.classList.remove('modal-open');
			document.body.style.overflow = 'auto';
			document.body.style.paddingRight = '0';
		""")
		
		# Then try clicking accept buttons as backup
		cookie_selectors = [
			"#understandCookieConsent",
			"#acceptAllCookies",
			"button:has-text('Accept all cookies')",
			"button:has-text('Accept')",
			".cookie-accept"
		]
		
		for selector in cookie_selectors:
			try:
				if page.locator(selector).count() > 0:
					page.locator(selector).first.click(timeout=1000)
					page.wait_for_timeout(500)
					break
			except:
				continue
		
		page.wait_for_timeout(200)
	except:
		pass


def process_legislation_document(page, chrome_page, href, title, citation, prefix, tracking_data):
	"""Process a single legislation document (download, PDF, S3, track)"""
	# Create document key for tracking
	doc_key = f"{prefix}_{href}"
	
	# Create sanitized filename
	safe_filename = sanitize_filename(f"{citation}_{title}"[:150]) if citation else sanitize_filename(f"{title}"[:150])
	s3_key = f"{safe_filename}.pdf"
	
	# Log document being processed
	logger.info("=" * 80)
	logger.info(f"📄 PROCESSING DOCUMENT: {title}")
	if citation:
		logger.info(f"   Citation: {citation}")
	logger.info(f"   Filename: {safe_filename}.pdf")
	logger.info(f"   URL: {BASE_URL}{href}")
	logger.info("=" * 80)
	
	# Check local tracking
	if is_already_processed(tracking_data, doc_key):
		logger.info(f"⏭️  Skipping (already processed) - {title}")
		return False
	
	# Go to document page
	doc_url = f"{BASE_URL}{href}"
	try:
		logger.info(f"🌐 Navigating to document page...")
		try:
			page.goto(doc_url, wait_until="load", timeout=30000)
			logger.info(f"✅ Page loaded successfully")
		except Exception as e:
			logger.warning(f"⚠️  Navigation error: {e}")
			
		page.wait_for_load_state("domcontentloaded")
		page.wait_for_timeout(WAIT_MS)
		
		# Remove cookie modal immediately after page load
		force_remove_cookie_modal(page)
		
		# Check for CAPTCHA interruption
		if is_captcha_page(page):
			logger.warning(f"⚠️  CAPTCHA detected on document page!")
			if handle_captcha_interruption(page):
				logger.info(f"🔄 Resuming document processing after recovery...")
				# Retry navigation
				page.goto(doc_url, wait_until="load")
				page.wait_for_load_state("domcontentloaded")
				force_remove_cookie_modal(page)  # Remove again after recovery
			else:
				logger.error(f"❌ Could not recover from CAPTCHA. Skipping this doc.")
				return False

		# Extract content (checks for in-force status inside)
		logger.info(f"📝 Extracting document content...")
		doc_title, content_html = extract_document_content(page, href, title)
		
		if doc_title and content_html:
			pdf_path = os.path.join(OUTPUT_DIR, s3_key)
			logger.info(f"📄 Content extracted successfully")
			
			# Generate PDF using Chrome
			logger.info(f"🖨️  Generating PDF...")
			if create_pdf_from_html(chrome_page, doc_title, content_html, pdf_path):
				logger.info(f"✅ PDF generated: {pdf_path}")
				# Upload to S3
				logger.info(f"☁️  Uploading to S3...")
				if upload_to_s3(pdf_path, s3_key):
					logger.info(f"✅ Upload successful: s3://{S3_BUCKET_NAME}/{s3_key}")
					delete_local_file(pdf_path)
					mark_as_processed(tracking_data, {
						"key": doc_key,
						"title": title,
						"citation": citation,
						"href": href,
						"url": f"{BASE_URL}{href}",
						"s3_key": s3_key
					})
					logger.info(f"✅ DOCUMENT COMPLETED: {title}")
					delay_between_downloads()
					return True
				else:
					logger.error(f"❌ S3 upload failed for: {title}")
			else:
				logger.error(f"❌ PDF generation failed for: {title}")
		else:
			logger.warning(f"⚠️  Could not extract content for: {title}")
			
	except Exception as e:
		logger.error(f"❌ Error processing document {title}: {e}")
		import traceback
		logger.error(traceback.format_exc())
	
	return False


def extract_row_data(row):
	"""Extract main item and all sub-items (regulations, amendments, enabling statutes) from a table row"""
	try:
		return row.evaluate("""
			(row) => {
				const result = { main: null, sub_items: [] };

				// --- Extract main item ---
				const canliiLink = row.querySelector('a.canlii');
				if (!canliiLink) return result;

				const mainHref = canliiLink.getAttribute('href');
				const mainTitle = canliiLink.textContent.trim();

				// Get citation
				let citation = '';
				const decisionDateTd = row.querySelector('td.decisionDate');
				if (decisionDateTd) {
					citation = decisionDateTd.textContent.trim();
				} else {
					const nowrap = row.querySelector('td:first-child span.nowrap');
					if (nowrap) citation = nowrap.textContent.trim();
				}

				// Check if main item is repealed (Category 4 pattern: direct span with [Repealed...] in same td)
				let mainRepealed = false;
				const canliiTd = canliiLink.closest('td');
				if (canliiTd) {
					for (const child of canliiTd.childNodes) {
						if (child.nodeType === 1 && child.tagName === 'SPAN'
							&& !child.classList.contains('nowrap')
							&& !child.classList.contains('d-flex')
							&& !child.classList.contains('text-end')) {
							const txt = child.textContent.toLowerCase();
							if (txt.includes('repealed') || txt.includes('not in force') || txt.includes('spent')) {
								mainRepealed = true;
							}
						}
					}
				}

				result.main = {
					href: mainHref,
					title: mainTitle,
					citation: citation,
					is_repealed: mainRepealed
				};

				// --- Extract sub-items from dropdowns (regulations, amendments) ---
				// Handles: div[id^='regulation_'] (Categories 1,2) and div[id^='legislation_'] (Category 3)
				const dropdowns = row.querySelectorAll("div[id^='regulation_'], div[id^='legislation_']");
				for (const dropdown of dropdowns) {
					let currentSection = 'in_force';

					for (const child of dropdown.children) {
						if (child.tagName === 'DIV') {
							const text = child.textContent.toLowerCase().trim();
							if (text.includes('repealed') || text.includes('spent') || text.includes('not in force')) {
								currentSection = 'repealed';
							} else {
								currentSection = 'in_force';
							}
						} else if (child.tagName === 'UL' && currentSection === 'in_force') {
							const items = child.querySelectorAll('li');
							for (const item of items) {
								const link = item.querySelector('a[href]');
								if (link) {
									const nw = item.querySelector('span.nowrap');
									result.sub_items.push({
										href: link.getAttribute('href'),
										title: link.textContent.trim(),
										citation: nw ? nw.textContent.trim() : '',
										type: 'sub_item'
									});
								}
							}
						}
					}
				}

				// --- Extract enabling statute from second column (Category 4) ---
				const tds = row.querySelectorAll('td');
				if (tds.length >= 2) {
					const secondTd = tds[1];
					// Category 4: second td has direct <a> links (not a.canlii, not inside dropdown)
					const hasCanliiInSecond = secondTd.querySelector('a.canlii');
					const hasDropdown = secondTd.querySelector("div[id^='regulation_'], div[id^='legislation_']");

					if (!hasCanliiInSecond && !hasDropdown) {
						const links = secondTd.querySelectorAll('a[href]');
						for (const link of links) {
							const href = link.getAttribute('href');
							if (href && href.includes('/laws/')) {
								const nw = secondTd.querySelector('span.nowrap');
								result.sub_items.push({
									href: href,
									title: link.textContent.trim(),
									citation: nw ? nw.textContent.trim() : '',
									type: 'enabling_statute'
								});
							}
						}
					}
				}

				return result;
			}
		""")
	except Exception as e:
		print(f"  Error in extract_row_data: {e}")
		return {"main": None, "sub_items": []}


def process_category_page(page, chrome_page, tracking_data, category_url):
	"""Process all items in a category page in real-time"""
	try:
		# Remove cookie modal first
		force_remove_cookie_modal(page)
		
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
					page.wait_for_timeout(2000)
					
					# Quick check for CAPTCHA during pagination
					if is_captcha_page(page):
						print("⚠️ CAPTCHA detected during pagination!")
						if handle_captcha_interruption(page):
							print("    🔄 Resuming pagination after recovery...")
							page.goto(category_url, wait_until="load")
							page.wait_for_timeout(2000)
						else:
							print("    Waiting for manual CAPTCHA solve...")
							while is_captcha_page(page):
								page.wait_for_timeout(5000)
						page.wait_for_timeout(2000)
				else:
					break
			except Exception:
				break
		
		print("  All records loaded, starting extraction...")
		
		# IMPORTANT: Collect ALL item data FIRST before navigating away
		# This prevents stale element references when we navigate to document pages
		# Uses JavaScript evaluation to also extract sub-items (regulations, amendments, enabling statutes)
		items_to_process = []
		rows = page.locator("#legislationsContainer tr").all()
		total_rows = len(rows)
		print(f"Found {total_rows} legislation rows to scan")
		
		for row in rows:
			try:
				row_data = extract_row_data(row)
				if row_data and row_data.get("main"):
					items_to_process.append(row_data)
			except Exception as e:
				print(f"  Error extracting row data: {e}")
				continue
		
		# Count total documents (main + sub-items)
		total_main = len(items_to_process)
		total_subs = sum(len(item.get("sub_items", [])) for item in items_to_process)
		print(f"  Collected {total_main} main items + {total_subs} sub-items = {total_main + total_subs} total documents")
		
		processed_count = 0
		
		# Now process each item - we have all the data we need stored
		for i, item in enumerate(items_to_process, 1):
			try:
				main = item["main"]
				sub_items = item.get("sub_items", [])
				
				# Skip repealed main items (Category 4 pattern: marked in the list itself)
				if main.get("is_repealed"):
					print(f"\n  ⏭️  Skipping item {i}/{len(items_to_process)} (repealed in list): {main['title']}")
					save_skipped_document({
						"title": main["title"],
						"href": main["href"],
						"url": f"{BASE_URL}{main['href']}",
						"reason": "Repealed, spent or not in force (marked in category list)"
					})
					continue
				
				# Check if already processed to resume directly
				main_key = f"main_{main['href']}"
				main_processed = is_already_processed(tracking_data, main_key)
				
				# Check if all sub-items are processed
				all_subs_processed = True
				for sub in sub_items:
					sub_type = sub.get("type", "sub_item")
					sub_key = f"{sub_type}_{sub['href']}"
					if not is_already_processed(tracking_data, sub_key):
						all_subs_processed = False
						break
				
				if main_processed and all_subs_processed:
					continue
				
				print(f"\n  Processing item {i}/{len(items_to_process)}: {main['title']}")
				if sub_items:
					print(f"    ({len(sub_items)} sub-items: regulations/amendments/enabling statutes)")
				
				# Process Main Document
				if process_legislation_document(page, chrome_page, main["href"], main["title"], main["citation"], "main", tracking_data):
					processed_count += 1
				
				# Process sub-items (regulations, amendments, enabling statutes)
				for j, sub in enumerate(sub_items, 1):
					sub_type = sub.get("type", "sub_item")
					print(f"    Sub-item {j}/{len(sub_items)} [{sub_type}]: {sub['title']}")
					if process_legislation_document(page, chrome_page, sub["href"], sub["title"], sub["citation"], sub_type, tracking_data):
						processed_count += 1
				
				# After processing all items for this row, navigate back to category page
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
				page.wait_for_timeout(1000)
				
				# Check for CAPTCHA after returning to category page
				if is_captcha_page(page):
					print("    ⚠️  CAPTCHA detected after returning to category page!")
					if handle_captcha_interruption(page):
						print("    🔄 Resuming category page processing after recovery...")
						page.goto(category_url, wait_until="load")
						page.wait_for_load_state("networkidle")
						force_remove_cookie_modal(page)
					else:
						print("    ❌ Could not recover from CAPTCHA. Please solve manually...")
						while is_captcha_page(page):
							page.wait_for_timeout(5000)
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
					
					# Check for CAPTCHA after error recovery navigation
					if is_captcha_page(page):
						print("    ⚠️  CAPTCHA detected during error recovery!")
						if handle_captcha_interruption(page):
							page.goto(category_url, wait_until="load")
							page.wait_for_load_state("networkidle")
						else:
							print("    ❌ Waiting for manual CAPTCHA solve...")
							while is_captcha_page(page):
								page.wait_for_timeout(5000)
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


def process_constitutional_acts(page, chrome_page, tracking_data):
	"""Process Constitutional Acts category - handles documents with nested regulations"""
	category_url = f"{BASE_URL}/ca/laws/const"
	category_name = "Constitutional Acts"
	
	print(f"\n{'='*80}")
	print(f"PROCESSING: {category_name}")
	print(f"URL: {category_url}")
	print(f"{'='*80}\n")
	
	try:
		# Navigate to the category page
		page.goto(category_url, wait_until="load")
		page.wait_for_load_state("networkidle")
		page.wait_for_timeout(2000)
		force_remove_cookie_modal(page)
		
		# Check for CAPTCHA
		if is_captcha_page(page):
			print("⚠️  CAPTCHA detected on Constitutional Acts page!")
			if handle_captcha_interruption(page):
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
			else:
				print("Please solve CAPTCHA manually...")
				while is_captcha_page(page):
					page.wait_for_timeout(5000)
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
		
		# Wait for table to load
		page.wait_for_selector("#filterableList tbody tr", timeout=15000)
		print(f"✓ {category_name} table loaded")
		
		# Collect all document data first (before navigation)
		all_documents = []
		rows = page.locator("#filterableList tbody tr").all()
		print(f"Found {len(rows)} rows in {category_name}")
		
		for idx, row in enumerate(rows, 1):
			try:
				# Extract main document info
				citation_cell = row.locator("td").nth(0)
				title_cell = row.locator("td").nth(1)
				
				citation = citation_cell.inner_text().strip()
				main_link = title_cell.locator("a.canlii").first
				
				if main_link.count() == 0:
					continue
					
				main_href = main_link.get_attribute("href")
				main_title = main_link.inner_text().strip()
				
				doc_info = {
					"citation": citation,
					"href": main_href,
					"title": main_title,
					"regulations": []
				}
				
				# Check if row has expandable regulations
				regulation_toggle = title_cell.locator("a.pointer.text-nowrap")
				if regulation_toggle.count() > 0:
					toggle_text = regulation_toggle.inner_text()
					print(f"  Row {idx}: {main_title} - Found {toggle_text}")
					
					# Click to expand (the div will appear/become visible)
					try:
						regulation_toggle.click()
						page.wait_for_timeout(500)
						print(f"    Expanded regulations...")
					except Exception as e:
						print(f"    ⚠️  Could not expand: {e}")
					
					# Now parse the expanded regulations div (it should be visible now)
					regulation_div = title_cell.locator("div[id^='regulation_']").first
					if regulation_div.count() > 0 and regulation_div.is_visible():
						# The structure is: <div> (section header) followed by <ul> (list of items)
						# We iterate through children and track which section we're in
						children = regulation_div.locator("> *").all()
						current_section = None
						
						for child in children:
							tag_name = child.evaluate("el => el.tagName.toLowerCase()")
							
							if tag_name == "div":
								# This is a section header
								section_text = child.inner_text().lower().strip()
								
								if "in force" in section_text:
									current_section = "in_force"
									print(f"      [Section: In force]")
								elif any(keyword in section_text for keyword in ["repealed", "spent", "not in force"]):
									current_section = "repealed"
									print(f"      [Section: Repealed/Not in force]")
							
							elif tag_name == "ul":
								# This is a list - process based on current section
								if current_section == "in_force":
									items = child.locator("li").all()
									for item in items:
										link = item.locator("a").first
										if link.count() > 0:
											reg_href = link.get_attribute("href")
											reg_title = link.inner_text().strip()
											reg_citation_span = item.locator("span.nowrap")
											reg_citation = reg_citation_span.inner_text().strip() if reg_citation_span.count() > 0 else ""
											
											doc_info["regulations"].append({
												"href": reg_href,
												"title": reg_title,
												"citation": reg_citation
											})
											print(f"        ✓ {reg_title} ({reg_citation})")
								elif current_section == "repealed":
									# Skip and log
									items = child.locator("li").all()
									for item in items:
										link = item.locator("a").first
										if link.count() > 0:
											skipped_title = link.inner_text().strip()
											print(f"        ⏭️  Skipped: {skipped_title}")
					else:
						print(f"    ⚠️  Could not find regulation div after expansion")
				else:
					print(f"  Row {idx}: {main_title} - No regulations")
				
				all_documents.append(doc_info)
				
			except Exception as e:
				print(f"  ⚠️  Error extracting row {idx}: {e}")
				continue
		
		print(f"\n✓ Collected {len(all_documents)} main documents")
		total_regulations = sum(len(doc["regulations"]) for doc in all_documents)
		print(f"✓ Total regulations to process: {total_regulations}")
		print(f"✓ Total documents: {len(all_documents) + total_regulations}\n")
		
		# Now process each document
		processed_count = 0
		
		for idx, doc in enumerate(all_documents, 1):
			print(f"\n[{idx}/{len(all_documents)}] Processing: {doc['title']}")
			
			# Process main document
			main_key = f"main_{doc['href']}"
			if is_already_processed(tracking_data, main_key):
				print(f"  ⏭️  Main document already processed")
			else:
				if process_legislation_document(page, chrome_page, doc["href"], doc["title"], doc["citation"], "main", tracking_data):
					processed_count += 1
			
			# Process regulations
			for reg_idx, reg in enumerate(doc["regulations"], 1):
				print(f"  [{reg_idx}/{len(doc['regulations'])}] Regulation: {reg['title']}")
				reg_key = f"sub_item_{reg['href']}"
				if is_already_processed(tracking_data, reg_key):
					print(f"    ⏭️  Already processed")
				else:
					if process_legislation_document(page, chrome_page, reg["href"], reg["title"], reg["citation"], "sub_item", tracking_data):
						processed_count += 1
			
			# Return to category page after processing each main document and its regulations
			page.goto(category_url, wait_until="load")
			page.wait_for_load_state("networkidle")
			page.wait_for_timeout(1000)
			
			# Check for CAPTCHA after returning
			if is_captcha_page(page):
				print("    ⚠️  CAPTCHA detected after returning!")
				if handle_captcha_interruption(page):
					page.goto(category_url, wait_until="load")
					page.wait_for_load_state("networkidle")
				else:
					print("    Please solve CAPTCHA manually...")
					while is_captcha_page(page):
						page.wait_for_timeout(5000)
					page.goto(category_url, wait_until="load")
					page.wait_for_load_state("networkidle")
		
		print(f"\n{'='*80}")
		print(f"✓ {category_name} COMPLETE - Processed {processed_count} documents")
		print(f"{'='*80}\n")
		
		return processed_count
		
	except Exception as e:
		print(f"\n❌ Error processing {category_name}: {e}")
		import traceback
		traceback.print_exc()
		return 0


def process_consolidated_statutes(page, chrome_page, tracking_data):
	"""Process Consolidated Statutes category - handles large dataset with pagination"""
	category_url = f"{BASE_URL}/ca/laws/stat"
	category_name = "Consolidated Statutes"
	
	print(f"\n{'='*80}")
	print(f"PROCESSING: {category_name}")
	print(f"URL: {category_url}")
	print(f"{'='*80}\n")
	
	try:
		# Navigate to the category page
		page.goto(category_url, wait_until="load")
		page.wait_for_load_state("networkidle")
		page.wait_for_timeout(2000)
		force_remove_cookie_modal(page)
		
		# Check for CAPTCHA
		if is_captcha_page(page):
			print("⚠️  CAPTCHA detected on Consolidated Statutes page!")
			if handle_captcha_interruption(page):
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
			else:
				print("Please solve CAPTCHA manually...")
				while is_captcha_page(page):
					page.wait_for_timeout(5000)
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
		
		# Wait for table to load
		page.wait_for_selector("#filterableList tbody tr", timeout=15000)
		print(f"✓ {category_name} table loaded")
		
		# Click "Show more results" until all items are loaded
		print("Loading all results (clicking 'Show more results')...")
		click_count = 0
		while True:
			try:
				show_more = page.locator("span.showMoreResults")
				if show_more.count() > 0 and show_more.is_visible():
					click_count += 1
					print(f"  Clicking 'Show more results' (click #{click_count})...")
					show_more.click()
					page.wait_for_timeout(2000)
					
					# Check for CAPTCHA during pagination
					if is_captcha_page(page):
						print("⚠️  CAPTCHA detected during pagination!")
						if handle_captcha_interruption(page):
							page.goto(category_url, wait_until="load")
							page.wait_for_load_state("networkidle")
							page.wait_for_timeout(2000)
						else:
							print("Please solve CAPTCHA manually...")
							while is_captcha_page(page):
								page.wait_for_timeout(5000)
							page.goto(category_url, wait_until="load")
							page.wait_for_load_state("networkidle")
							page.wait_for_timeout(2000)
				else:
					break
			except Exception as e:
				print(f"  No more results to load (or error: {e})")
				break
		
		print(f"✓ All results loaded after {click_count} pagination clicks")
		
		# Collect all document data first (before navigation)
		all_documents = []
		rows = page.locator("#filterableList tbody tr").all()
		print(f"Found {len(rows)} rows in {category_name}")
		
		for idx, row in enumerate(rows, 1):
			if idx % 50 == 0:
				print(f"  Parsing row {idx}/{len(rows)}...")
			
			try:
				# Extract main document info
				citation_cell = row.locator("td").nth(0)
				title_cell = row.locator("td").nth(1)
				
				citation = citation_cell.inner_text().strip()
				
				# Get the main span wrapper
				main_span = title_cell.locator("span.d-flex").first
				if main_span.count() == 0:
					continue
				
				# Get the main link
				main_link = main_span.locator("a.canlii").first
				if main_link.count() == 0:
					continue
					
				main_href = main_link.get_attribute("href")
				main_title = main_link.inner_text().strip()
				
				# Check if main act is repealed (has "[Repealed, spent or not in force]" text)
				main_is_repealed = False
				repealed_indicator = main_span.locator("span", has_text="Repealed, spent or not in force")
				if repealed_indicator.count() > 0:
					main_is_repealed = True
				
				doc_info = {
					"citation": citation,
					"href": main_href,
					"title": main_title,
					"is_repealed": main_is_repealed,
					"regulations_in_force": [],
					"regulations_repealed": []
				}
				
				# Check if row has expandable regulations
				regulation_toggle = title_cell.locator("a.pointer.text-nowrap")
				if regulation_toggle.count() > 0:
					# Click to expand
					try:
						regulation_toggle.click()
						page.wait_for_timeout(300)
					except Exception as e:
						pass
					
					# Parse the expanded regulations div
					regulation_div = title_cell.locator("div[id^='regulation_']").first
					if regulation_div.count() > 0 and regulation_div.is_visible():
						# Iterate through children and track which section we're in
						children = regulation_div.locator("> *").all()
						current_section = None
						
						for child in children:
							tag_name = child.evaluate("el => el.tagName.toLowerCase()")
							
							if tag_name == "div":
								# Section header
								section_text = child.inner_text().lower().strip()
								
								if "in force" in section_text:
									current_section = "in_force"
								elif any(keyword in section_text for keyword in ["repealed", "spent", "not in force"]):
									current_section = "repealed"
							
							elif tag_name == "ul":
								# Process list based on current section
								items = child.locator("li").all()
								for item in items:
									link = item.locator("a").first
									if link.count() > 0:
										reg_href = link.get_attribute("href")
										reg_title = link.inner_text().strip()
										reg_citation_span = item.locator("span.nowrap")
										reg_citation = reg_citation_span.inner_text().strip() if reg_citation_span.count() > 0 else ""
										
										reg_data = {
											"href": reg_href,
											"title": reg_title,
											"citation": reg_citation
										}
										
										if current_section == "in_force":
											doc_info["regulations_in_force"].append(reg_data)
										elif current_section == "repealed":
											doc_info["regulations_repealed"].append(reg_data)
				
				all_documents.append(doc_info)
				
			except Exception as e:
				print(f"  ⚠️  Error extracting row {idx}: {e}")
				continue
		
		print(f"\n✓ Collected {len(all_documents)} main documents")
		total_in_force_regs = sum(len(doc["regulations_in_force"]) for doc in all_documents)
		total_repealed_regs = sum(len(doc["regulations_repealed"]) for doc in all_documents)
		repealed_main_count = sum(1 for doc in all_documents if doc["is_repealed"])
		
		print(f"✓ Main acts to process: {len(all_documents) - repealed_main_count}")
		print(f"✓ Main acts repealed: {repealed_main_count}")
		print(f"✓ Regulations (in force): {total_in_force_regs}")
		print(f"✓ Regulations (repealed): {total_repealed_regs}\n")
		
		# Now process each document
		processed_count = 0
		deleted_count = 0
		
		for idx, doc in enumerate(all_documents, 1):
			if idx % 50 == 0:
				print(f"\n[Progress: {idx}/{len(all_documents)}]")
			
			try:
				main_key = f"main_{doc['href']}"
				safe_filename = sanitize_filename(f"{doc['citation']}_{doc['title']}"[:150]) if doc['citation'] else sanitize_filename(f"{doc['title']}"[:150])
				main_s3_key = f"{safe_filename}.pdf"
				
				# Handle repealed main act
				if doc['is_repealed']:
					print(f"\n[{idx}/{len(all_documents)}] 🗑️  REPEALED: {doc['title']}")
					
					# Delete from S3 if exists
					if delete_from_s3(main_s3_key):
						deleted_count += 1
					
					# Remove from tracking
					remove_from_processed(tracking_data, main_key)
					
					# Save to skipped file
					save_skipped_document({
						"title": doc["title"],
						"href": doc["href"],
						"url": f"{BASE_URL}{doc['href']}",
						"citation": doc["citation"],
						"reason": "Repealed, spent or not in force (marked in category list)"
					})
					
					# Also handle repealed regulations
					for reg in doc["regulations_repealed"]:
						reg_key = f"sub_item_{reg['href']}"
						reg_safe_filename = sanitize_filename(f"{reg['citation']}_{reg['title']}"[:150]) if reg['citation'] else sanitize_filename(f"{reg['title']}"[:150])
						reg_s3_key = f"{reg_safe_filename}.pdf"
						
						print(f"  🗑️  Repealed regulation: {reg['title']}")
						if delete_from_s3(reg_s3_key):
							deleted_count += 1
						remove_from_processed(tracking_data, reg_key)
					
					continue
				
				# Process in-force main act
				print(f"\n[{idx}/{len(all_documents)}] Processing: {doc['title']}")
				if doc['regulations_in_force']:
					print(f"  ({len(doc['regulations_in_force'])} regulations)")
				
				# Check if main already processed
				if is_already_processed(tracking_data, main_key):
					print(f"  ⏭️  Main document already processed")
				else:
					if process_legislation_document(page, chrome_page, doc["href"], doc["title"], doc["citation"], "main", tracking_data):
						processed_count += 1
				
				# Process in-force regulations
				for reg_idx, reg in enumerate(doc["regulations_in_force"], 1):
					reg_key = f"sub_item_{reg['href']}"
					if is_already_processed(tracking_data, reg_key):
						if idx % 50 == 0:  # Only log for progress updates
							print(f"    [{reg_idx}/{len(doc['regulations_in_force'])}] ⏭️  {reg['title']}")
					else:
						print(f"    [{reg_idx}/{len(doc['regulations_in_force'])}] Regulation: {reg['title']}")
						if process_legislation_document(page, chrome_page, reg["href"], reg["title"], reg["citation"], "sub_item", tracking_data):
							processed_count += 1
				
				# Handle repealed regulations (delete from S3)
				for reg in doc["regulations_repealed"]:
					reg_key = f"sub_item_{reg['href']}"
					reg_safe_filename = sanitize_filename(f"{reg['citation']}_{reg['title']}"[:150]) if reg['citation'] else sanitize_filename(f"{reg['title']}"[:150])
					reg_s3_key = f"{reg_safe_filename}.pdf"
					
					# Only log and delete if it was previously processed
					if is_already_processed(tracking_data, reg_key):
						print(f"    🗑️  Removing repealed regulation: {reg['title']}")
						if delete_from_s3(reg_s3_key):
							deleted_count += 1
						remove_from_processed(tracking_data, reg_key)
						
						save_skipped_document({
							"title": reg["title"],
							"href": reg["href"],
							"url": f"{BASE_URL}{reg['href']}",
							"citation": reg["citation"],
							"reason": "Repealed, spent or not in force"
						})
				
				# Return to category page after processing
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
				page.wait_for_timeout(800)
				
				# Check for CAPTCHA after returning
				if is_captcha_page(page):
					print("    ⚠️  CAPTCHA detected after returning!")
					if handle_captcha_interruption(page):
						page.goto(category_url, wait_until="load")
						page.wait_for_load_state("networkidle")
					else:
						print("    Please solve CAPTCHA manually...")
						while is_captcha_page(page):
							page.wait_for_timeout(5000)
						page.goto(category_url, wait_until="load")
						page.wait_for_load_state("networkidle")
				
			except Exception as e:
				print(f"  ⚠️  Error processing document {idx}: {e}")
				# Try to recover
				try:
					page.goto(category_url, wait_until="load")
					page.wait_for_load_state("networkidle")
					page.wait_for_timeout(1000)
				except:
					pass
				continue
		
		print(f"\n{'='*80}")
		print(f"✓ {category_name} COMPLETE")
		print(f"  Downloaded: {processed_count} documents")
		print(f"  Deleted (repealed): {deleted_count} documents")
		print(f"{'='*80}\n")
		
		return processed_count
		
	except Exception as e:
		print(f"\n❌ Error processing {category_name}: {e}")
		import traceback
		traceback.print_exc()
		return 0


def process_annual_statutes(page, chrome_page, tracking_data):
	"""Process Annual Statutes category - year-based legislation"""
	category_url = f"{BASE_URL}/ca/laws/astat"
	category_name = "Annual Statutes"
	
	print(f"\n{'='*80}")
	print(f"PROCESSING: {category_name}")
	print(f"URL: {category_url}")
	print(f"{'='*80}\n")
	
	try:
		# Navigate to the category page
		page.goto(category_url, wait_until="load")
		page.wait_for_load_state("networkidle")
		page.wait_for_timeout(2000)
		force_remove_cookie_modal(page)
		
		# Check for CAPTCHA
		if is_captcha_page(page):
			print("⚠️  CAPTCHA detected on Annual Statutes page!")
			if handle_captcha_interruption(page):
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
			else:
				print("Please solve CAPTCHA manually...")
				while is_captcha_page(page):
					page.wait_for_timeout(5000)
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
		
		# Wait for table to load
		page.wait_for_selector("#filterableList tbody tr", timeout=15000)
		print(f"✓ {category_name} table loaded")
		
		# Click "Show more results" until all items are loaded
		print("Loading all results (clicking 'Show more results')...")
		click_count = 0
		while True:
			try:
				show_more = page.locator("span.showMoreResults")
				if show_more.count() > 0 and show_more.is_visible():
					click_count += 1
					print(f"  Clicking 'Show more results' (click #{click_count})...")
					show_more.click()
					page.wait_for_timeout(2000)
					
					# Check for CAPTCHA during pagination
					if is_captcha_page(page):
						print("⚠️  CAPTCHA detected during pagination!")
						if handle_captcha_interruption(page):
							page.goto(category_url, wait_until="load")
							page.wait_for_load_state("networkidle")
							page.wait_for_timeout(2000)
						else:
							print("Please solve CAPTCHA manually...")
							while is_captcha_page(page):
								page.wait_for_timeout(5000)
							page.goto(category_url, wait_until="load")
							page.wait_for_load_state("networkidle")
							page.wait_for_timeout(2000)
				else:
					break
			except Exception as e:
				print(f"  No more results to load (or error: {e})")
				break
		
		print(f"✓ All results loaded after {click_count} pagination clicks")
		
		# Collect all document data first (before navigation)
		all_documents = []
		rows = page.locator("#filterableList tbody tr").all()
		print(f"Found {len(rows)} rows in {category_name}")
		
		for idx, row in enumerate(rows, 1):
			if idx % 50 == 0:
				print(f"  Parsing row {idx}/{len(rows)}...")
			
			try:
				# Extract main document info
				citation_cell = row.locator("td").nth(0)
				title_cell = row.locator("td").nth(1)
				
				citation = citation_cell.inner_text().strip()
				
				# Get the main span wrapper
				main_span = title_cell.locator("span.d-flex").first
				if main_span.count() == 0:
					continue
				
				# Get the main link
				main_link = main_span.locator("a.canlii").first
				if main_link.count() == 0:
					continue
					
				main_href = main_link.get_attribute("href")
				main_title = main_link.inner_text().strip()
				
				# Extract bill info if present (e.g., "Bill C-40, assented to 2024-12-17")
				bill_info = ""
				bill_span = main_span.locator("span").nth(1)
				if bill_span.count() > 0:
					bill_info = bill_span.inner_text().strip()
				
				doc_info = {
					"citation": citation,
					"href": main_href,
					"title": main_title,
					"bill_info": bill_info,
					"amended_statutes": [],
					"amended_regulations": []
				}
				
				# Check if row has expandable amendments
				amendment_toggle = title_cell.locator("a.pointer.text-nowrap")
				if amendment_toggle.count() > 0:
					# Click to expand
					try:
						amendment_toggle.click()
						page.wait_for_timeout(300)
					except Exception as e:
						pass
					
					# Parse the expanded amendments div (uses 'legislation_' prefix, not 'regulation_')
					amendment_div = title_cell.locator("div[id^='legislation_']").first
					if amendment_div.count() > 0 and amendment_div.is_visible():
						# Iterate through children and track which section we're in
						children = amendment_div.locator("> *").all()
						current_section = None
						
						for child in children:
							tag_name = child.evaluate("el => el.tagName.toLowerCase()")
							
							if tag_name == "div":
								# Section header
								section_text = child.inner_text().lower().strip()
								
								if "amended statutes" in section_text:
									current_section = "amended_statutes"
								elif "amended regulations" in section_text:
									current_section = "amended_regulations"
							
							elif tag_name == "ul":
								# Process list based on current section
								items = child.locator("li").all()
								for item in items:
									link = item.locator("a").first
									if link.count() > 0:
										ref_href = link.get_attribute("href")
										ref_title = link.inner_text().strip()
										ref_citation_span = item.locator("span.nowrap")
										ref_citation = ref_citation_span.inner_text().strip() if ref_citation_span.count() > 0 else ""
										
										ref_data = {
											"href": ref_href,
											"title": ref_title,
											"citation": ref_citation
										}
										
										if current_section == "amended_statutes":
											doc_info["amended_statutes"].append(ref_data)
										elif current_section == "amended_regulations":
											doc_info["amended_regulations"].append(ref_data)
				
				all_documents.append(doc_info)
				
			except Exception as e:
				print(f"  ⚠️  Error extracting row {idx}: {e}")
				continue
		
		print(f"\n✓ Collected {len(all_documents)} annual statutes")
		total_amended_statutes = sum(len(doc["amended_statutes"]) for doc in all_documents)
		total_amended_regulations = sum(len(doc["amended_regulations"]) for doc in all_documents)
		
		print(f"✓ References {total_amended_statutes} amended statutes (not downloaded - covered in Consolidated Statutes)")
		print(f"✓ References {total_amended_regulations} amended regulations (not downloaded - covered in Regulations)\n")
		
		# Now process each document - only download the main annual statute
		processed_count = 0
		
		for idx, doc in enumerate(all_documents, 1):
			if idx % 50 == 0:
				print(f"\n[Progress: {idx}/{len(all_documents)}]")
			
			try:
				main_key = f"main_{doc['href']}"
				
				# Check if already processed
				if is_already_processed(tracking_data, main_key):
					if idx % 50 == 0:  # Only log for progress updates
						print(f"[{idx}/{len(all_documents)}] ⏭️  {doc['title']}")
					continue
				
				# Process the annual statute
				print(f"\n[{idx}/{len(all_documents)}] Processing: {doc['title']}")
				if doc['bill_info']:
					print(f"  {doc['bill_info']}")
				
				# Log amendments for reference (but don't download them)
				if doc['amended_statutes']:
					print(f"  ℹ️  Amends {len(doc['amended_statutes'])} statute(s):")
					for stat in doc['amended_statutes'][:3]:  # Show first 3
						print(f"    • {stat['title']}")
					if len(doc['amended_statutes']) > 3:
						print(f"    • ... and {len(doc['amended_statutes']) - 3} more")
				
				if doc['amended_regulations']:
					print(f"  ℹ️  Amends {len(doc['amended_regulations'])} regulation(s)")
				
				# Download only the main annual statute
				if process_legislation_document(page, chrome_page, doc["href"], doc["title"], doc["citation"], "main", tracking_data):
					processed_count += 1
				
				# Return to category page after processing
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
				page.wait_for_timeout(800)
				
				# Check for CAPTCHA after returning
				if is_captcha_page(page):
					print("    ⚠️  CAPTCHA detected after returning!")
					if handle_captcha_interruption(page):
						page.goto(category_url, wait_until="load")
						page.wait_for_load_state("networkidle")
					else:
						print("    Please solve CAPTCHA manually...")
						while is_captcha_page(page):
							page.wait_for_timeout(5000)
						page.goto(category_url, wait_until="load")
						page.wait_for_load_state("networkidle")
				
			except Exception as e:
				print(f"  ⚠️  Error processing document {idx}: {e}")
				# Try to recover
				try:
					page.goto(category_url, wait_until="load")
					page.wait_for_load_state("networkidle")
					page.wait_for_timeout(1000)
				except:
					pass
				continue
		
		print(f"\n{'='*80}")
		print(f"✓ {category_name} COMPLETE")
		print(f"  Downloaded: {processed_count} annual statutes")
		print(f"{'='*80}\n")
		
		return processed_count
		
	except Exception as e:
		print(f"\n❌ Error processing {category_name}: {e}")
		import traceback
		traceback.print_exc()
		return 0


def process_regulations(page, chrome_page, tracking_data):
	"""Process Regulations category - largest dataset with 5000+ regulations"""
	category_url = f"{BASE_URL}/ca/laws/regu"
	category_name = "Regulations"
	
	print(f"\n{'='*80}")
	print(f"PROCESSING: {category_name}")
	print(f"URL: {category_url}")
	print(f"{'='*80}\n")
	
	try:
		# Navigate to the category page
		page.goto(category_url, wait_until="load")
		page.wait_for_load_state("networkidle")
		page.wait_for_timeout(2000)
		force_remove_cookie_modal(page)
		
		# Check for CAPTCHA
		if is_captcha_page(page):
			print("⚠️  CAPTCHA detected on Regulations page!")
			if handle_captcha_interruption(page):
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
			else:
				print("Please solve CAPTCHA manually...")
				while is_captcha_page(page):
					page.wait_for_timeout(5000)
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
		
		# Wait for table to load
		page.wait_for_selector("#filterableList tbody tr", timeout=15000)
		print(f"✓ {category_name} table loaded")
		
		# Click "Show more results" until all items are loaded
		print("Loading all results (clicking 'Show more results')...")
		click_count = 0
		while True:
			try:
				show_more = page.locator("span.showMoreResults")
				if show_more.count() > 0 and show_more.is_visible():
					click_count += 1
					print(f"  Clicking 'Show more results' (click #{click_count})...")
					show_more.click()
					page.wait_for_timeout(2000)
					
					# Check for CAPTCHA during pagination
					if is_captcha_page(page):
						print("⚠️  CAPTCHA detected during pagination!")
						if handle_captcha_interruption(page):
							page.goto(category_url, wait_until="load")
							page.wait_for_load_state("networkidle")
							page.wait_for_timeout(2000)
						else:
							print("Please solve CAPTCHA manually...")
							while is_captcha_page(page):
								page.wait_for_timeout(5000)
							page.goto(category_url, wait_until="load")
							page.wait_for_load_state("networkidle")
							page.wait_for_timeout(2000)
				else:
					break
			except Exception as e:
				print(f"  No more results to load (or error: {e})")
				break
		
		print(f"✓ All results loaded after {click_count} pagination clicks")
		
		# Collect all document data first (before navigation)
		all_regulations = []
		rows = page.locator("#filterableList tbody tr").all()
		print(f"Found {len(rows)} rows in {category_name}")
		
		for idx, row in enumerate(rows, 1):
			if idx % 100 == 0:
				print(f"  Parsing row {idx}/{len(rows)}...")
			
			try:
				# Extract regulation info from first column
				first_td = row.locator("td").nth(0)
				
				# Get the main regulation link
				main_link = first_td.locator("a.canlii").first
				if main_link.count() == 0:
					continue
					
				reg_href = main_link.get_attribute("href")
				reg_title = main_link.inner_text().strip()
				
				# Get citation from nowrap span
				citation_span = first_td.locator("span.nowrap").first
				reg_citation = citation_span.inner_text().strip() if citation_span.count() > 0 else ""
				
				# Check if regulation is repealed
				is_repealed = False
				repealed_spans = first_td.locator("span").all()
				for span in repealed_spans:
					span_text = span.inner_text().lower()
					if "repealed" in span_text or "spent" in span_text or "not in force" in span_text:
						is_repealed = True
						break
				
				# Extract enabling statute info from second column (for reference, not download)
				second_td = row.locator("td").nth(1)
				enabling_statute = None
				if second_td.count() > 0:
					enabling_link = second_td.locator("a").first
					if enabling_link.count() > 0:
						enabling_href = enabling_link.get_attribute("href")
						enabling_title = enabling_link.inner_text().strip()
						enabling_citation_span = second_td.locator("span.nowrap").first
						enabling_citation = enabling_citation_span.inner_text().strip() if enabling_citation_span.count() > 0 else ""
						
						enabling_statute = {
							"href": enabling_href,
							"title": enabling_title,
							"citation": enabling_citation
						}
				
				reg_info = {
					"href": reg_href,
					"title": reg_title,
					"citation": reg_citation,
					"is_repealed": is_repealed,
					"enabling_statute": enabling_statute
				}
				
				all_regulations.append(reg_info)
				
			except Exception as e:
				print(f"  ⚠️  Error extracting row {idx}: {e}")
				continue
		
		print(f"\n✓ Collected {len(all_regulations)} regulations")
		repealed_count = sum(1 for reg in all_regulations if reg["is_repealed"])
		active_count = len(all_regulations) - repealed_count
		print(f"✓ Active regulations: {active_count}")
		print(f"✓ Repealed regulations: {repealed_count} (will be deleted from S3 if present)\n")
		
		# Now process each regulation
		processed_count = 0
		deleted_count = 0
		skipped_repealed = 0
		skipped_already_done = 0
		
		for idx, reg in enumerate(all_regulations, 1):
			if idx % 100 == 0:
				print(f"\n[Progress: {idx}/{len(all_regulations)} | Downloaded: {processed_count} | Deleted: {deleted_count} | Skipped: {skipped_repealed + skipped_already_done}]")
			
			try:
				reg_key = f"sub_item_{reg['href']}"
				safe_filename = sanitize_filename(f"{reg['citation']}_{reg['title']}"[:150]) if reg['citation'] else sanitize_filename(f"{reg['title']}"[:150])
				reg_s3_key = f"{safe_filename}.pdf"
				
				# Handle repealed regulations - delete from S3 and remove from tracking
				if reg['is_repealed']:
					skipped_repealed += 1
					
					# Only show details if it was previously processed (needs deletion)
					was_processed = is_already_processed(tracking_data, reg_key)
					
					if was_processed:
						print(f"\n[{idx}/{len(all_regulations)}] 🗑️  REPEALED: {reg['title']}")
						
						# Delete from S3 if exists
						if delete_from_s3(reg_s3_key):
							deleted_count += 1
						
						# Remove from tracking
						remove_from_processed(tracking_data, reg_key)
					elif idx % 100 == 0:  # Only log at progress intervals if not previously processed
						print(f"[{idx}] ⏭️  Skipped (repealed, never downloaded): {reg['title']}")
					
					# Save to skipped file
					save_skipped_document({
						"title": reg["title"],
						"href": reg["href"],
						"url": f"{BASE_URL}{reg['href']}",
						"citation": reg["citation"],
						"reason": "Repealed, spent or not in force"
					})
					continue
				
				# Check if already processed
				if is_already_processed(tracking_data, reg_key):
					skipped_already_done += 1
					if idx % 100 == 0:  # Only log at progress intervals
						print(f"[{idx}] ⏭️  Already done: {reg['title']}")
					continue
				
				# Process the regulation
				print(f"\n[{idx}/{len(all_regulations)}] Processing: {reg['title']}")
				if reg['enabling_statute']:
					print(f"  ℹ️  Enabled by: {reg['enabling_statute']['title']}")
				
				# Download only the regulation (not the enabling statute)
				if process_legislation_document(page, chrome_page, reg["href"], reg["title"], reg["citation"], "sub_item", tracking_data):
					processed_count += 1
				
				# Return to category page after processing
				page.goto(category_url, wait_until="load")
				page.wait_for_load_state("networkidle")
				page.wait_for_timeout(800)
				
				# Check for CAPTCHA after returning
				if is_captcha_page(page):
					print("    ⚠️  CAPTCHA detected after returning!")
					if handle_captcha_interruption(page):
						page.goto(category_url, wait_until="load")
						page.wait_for_load_state("networkidle")
					else:
						print("    Please solve CAPTCHA manually...")
						while is_captcha_page(page):
							page.wait_for_timeout(5000)
						page.goto(category_url, wait_until="load")
						page.wait_for_load_state("networkidle")
				
			except Exception as e:
				print(f"  ⚠️  Error processing regulation {idx}: {e}")
				# Try to recover
				try:
					page.goto(category_url, wait_until="load")
					page.wait_for_load_state("networkidle")
					page.wait_for_timeout(1000)
				except:
					pass
				continue
		
		logger.info(f"\n{'='*80}")
		logger.info(f"✓ {category_name} COMPLETE")
		logger.info(f"  Downloaded: {processed_count} regulations")
		logger.info(f"  Deleted (repealed): {deleted_count} regulations")
		logger.info(f"  Skipped (repealed, never downloaded): {skipped_repealed - deleted_count}")
		logger.info(f"  Skipped (already done): {skipped_already_done}")
		logger.info(f"{'='*80}\n")
		
		return processed_count
		
	except Exception as e:
		logger.error(f"\n❌ Error processing {category_name}: {e}")
		import traceback
		logger.error(traceback.format_exc())
		return 0


def main():
	# Create output directory
	os.makedirs(OUTPUT_DIR, exist_ok=True)
	
	# Load tracking data for resume functionality
	tracking_data = load_tracking_data()
	logger.info(f"📊 Loaded tracking data: {len(tracking_data.get('processed_documents', []))} documents already processed")
	
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
			
		logger.info(f"Running on {system_os}, Headless: {is_headless}")

		logger.info("Launching Firefox browser...")
		browser = p.firefox.launch(
			headless=is_headless,
			args=get_firefox_launch_args(),
			firefox_user_prefs=get_firefox_user_prefs()
		)
		
		logger.info("Creating browser context...")
		context = browser.new_context(
			viewport={"width": 1920, "height": 1080},
			user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
			locale="en-US",
			timezone_id="America/Toronto",
			permissions=["geolocation"],
			geolocation={"latitude": 45.4215, "longitude": -75.6972} # Ottawa
		)
		
		# Inject all stealth scripts
		logger.info("Injecting stealth scripts...")
		for script in get_stealth_scripts():
			context.add_init_script(script)

		logger.info("Creating new page...")
		page = context.new_page()
		
		# Create Chrome browser for PDF generation
		logger.info("Launching Chrome browser for PDF generation...")
		chrome_browser = p.chromium.launch(headless=True)
		chrome_context = chrome_browser.new_context()
		chrome_page = chrome_context.new_page()
		
		# Add random mouse movement to simulate human behavior
		page.mouse.move(random.randint(100, 500), random.randint(100, 500))
		
		logger.info(f"Navigating to {START_URL}...")
		try:
			page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
			logger.info("Navigation completed successfully")
		except Exception as e:
			logger.warning(f"Navigation completed with warning: {e}")
			# Continue anyway - page might still be usable
		page.wait_for_timeout(WAIT_MS)
		
		# Remove cookie modal immediately after initial navigation
		force_remove_cookie_modal(page)
		
		# Check for CAPTCHA FIRST (DataDome appears before cookie consent)
		logger.info("\n🔍 Checking for CAPTCHA on initial page...")
		try:
			datadome_detected = is_datadome_captcha(page, silent=True)  # Silent to avoid spam
			canlii_detected = page.locator("#captchaTag").count() > 0
			logger.info(f"DataDome CAPTCHA: {'DETECTED' if datadome_detected else 'not found'}")
			logger.info(f"CanLII CAPTCHA: {'DETECTED' if canlii_detected else 'not found'}")
		except Exception as e:
			logger.error(f"Error during CAPTCHA check: {e}")
			datadome_detected = None
			canlii_detected = False
		
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
					logger.warning(f"Navigation timeout after CAPTCHA, continuing anyway: {e}")
				
			page.wait_for_timeout(WAIT_MS)
			
			# Remove cookie modal again after CAPTCHA solving
			force_remove_cookie_modal(page)
		
		# Handle cookie consent (only after CAPTCHA is solved)
		logger.info("Checking for cookie banner...")
		handle_cookie_consent(page)
		
		try:
			page.wait_for_load_state("load", timeout=10000)
		except:
			pass
		page.wait_for_timeout(WAIT_MS)
		
		# Process each legislation category individually with specialized handlers
		total_processed = 0
		
		logger.info("\n" + "="*80)
		logger.info("📚 LEGISLATION CRAWLER - CATEGORY-BY-CATEGORY PROCESSING")
		logger.info("="*80)
		logger.info("\nCategories to process:")
		logger.info("  1. Constitutional Acts")
		logger.info("  2. Consolidated Statutes")
		logger.info("  3. Annual Statutes")
		logger.info("  4. Regulations")
		logger.info("="*80 + "\n")
		
		# Category 1: Constitutional Acts
		try:
			count = process_constitutional_acts(page, chrome_page, tracking_data)
			total_processed += count
		except Exception as e:
			print(f"\n❌ Failed to process Constitutional Acts: {e}")
			import traceback
			traceback.print_exc()
		
		# Category 2: Consolidated Statutes
		try:
			count = process_consolidated_statutes(page, chrome_page, tracking_data)
			total_processed += count
		except Exception as e:
			print(f"\n❌ Failed to process Consolidated Statutes: {e}")
			import traceback
			traceback.print_exc()
		
		# Category 3: Annual Statutes
		try:
			count = process_annual_statutes(page, chrome_page, tracking_data)
			total_processed += count
		except Exception as e:
			print(f"\n❌ Failed to process Annual Statutes: {e}")
			import traceback
			traceback.print_exc()
		
		# Category 4: Regulations
		try:
			count = process_regulations(page, chrome_page, tracking_data)
			total_processed += count
		except Exception as e:
			logger.error(f"\n❌ Failed to process Regulations: {e}")
			import traceback
			logger.error(traceback.format_exc())
		
		logger.info("\n" + "="*80)
		logger.info("✅ SCRAPING COMPLETE")
		logger.info("="*80)
		logger.info(f"📊 Total documents downloaded: {total_processed}")
		logger.info(f"☁️  PDFs saved in S3: s3://{S3_BUCKET_NAME}/")
		logger.info(f"📝 Tracking file: {TRACKING_FILE}")
		logger.info("="*80 + "\n")
		
		chrome_browser.close()
		browser.close()


if __name__ == "__main__":
	main()
