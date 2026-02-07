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

# Load environment variables
load_dotenv()

BASE_URL = "https://www.canlii.org"
START_URL = "https://www.canlii.org/ca"
SECTION_TITLE = "Boards and Tribunals"
WAIT_MS = 2000

# Bedrock CAPTCHA solver configuration
BEDROCK_MODEL_ID = "qwen.qwen3-vl-235b-a22b"  # Qwen model for vision tasks
BEDROCK_REGION = os.getenv("AWS_REGION", "us-east-1")
MAX_CAPTCHA_ATTEMPTS = 3  # Maximum attempts to solve CAPTCHA
S3_BUCKET_NAME = "can-judgements"
TRACKING_FILE = "boards_tracking.json"


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


def is_captcha_page(page):
	"""Check if the current page is a CAPTCHA page (CanLII or DataDome)"""
	try:
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


def is_datadome_captcha(page):
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
						print(f"    🔴 DataDome detected in frame '{frame.name or frame.url}' via: {indicator}")
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
		# Find the frame containing the CAPTCHA
		captcha_frame = is_datadome_captcha(page)
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
			try:
				# Sometimes inputs load slowly
				page.wait_for_timeout(1000)
				inputs = captcha_frame.locator(".audio-captcha-inputs").all()
			except:
				pass
				
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
	
	# First, check for DataDome CAPTCHA (slider/audio type)
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
				page.wait_for_timeout(1000)
				
		except Exception as e:
			print(f"    ⚠️  Error during CAPTCHA solving: {e}")
			continue
	
	print("    ⚠️  Auto-solve failed after max attempts, waiting for manual input...")
	return False


def handle_captcha_interruption(page):
	"""
	Handle CAPTCHA detected during deep processing.
	Strategy: Go to Homepage -> Solve -> Return True so caller can retry.
	"""
	print("\n🛑 CAPTCHA INTERRUPTION DETECTED!")
	print(f"   Initiating recovery protocol...")
	
	try:
		# 1. Go to homepage (safest place to solve)
		print(f"   Navigating to homepage ({START_URL}) to solve...")
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
		print(f"    ❌ Download error: {e}")
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
			print(f"    ⏭️  Already in S3: s3://{S3_BUCKET_NAME}/{s3_key}")
			return True  # Return True so local file gets deleted
		
		s3_client = boto3.client(
			's3',
			aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
			aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
			region_name=os.getenv('AWS_REGION', 'us-east-1')
		)
		
		# Upload the file
		s3_client.upload_file(local_file_path, S3_BUCKET_NAME, s3_key)
		print(f"    ✓ Uploaded to S3: s3://{S3_BUCKET_NAME}/{s3_key}")
		return True
	except Exception as e:
		print(f"    ✗ S3 upload failed: {e}")
		return False


def delete_local_file(file_path):
	"""Delete a local file after successful upload"""
	try:
		if os.path.exists(file_path):
			os.remove(file_path)
			print(f"    🗑️  Deleted local file: {os.path.basename(file_path)}")
			return True
	except Exception as e:
		print(f"    ⚠️  Could not delete local file: {e}")
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
		print(f"Warning: Could not save tracking file: {e}")


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
			print(f"    ⏭️  Skipping (already processed): {decision_url.split('/')[-1]}")
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
		print(f"  Visiting Year: {year}")
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
			print(f"    ⚠️ No decisions table found for {year}")
			return

		rows = page.locator("#decisionsListing tr").all()
		print(f"    Found {len(rows)} decisions")
		
		# Collect decision URLs first
		decision_links = []
		for row in rows:
			link_loc = row.locator("a.canlii")
			if link_loc.count() > 0:
				href = link_loc.get_attribute("href")
				if href:
					decision_links.append(BASE_URL + href if href.startswith("/") else href)
		
		save_dir = os.path.join("downloads", board_name, year)
		
		for idx, url in enumerate(decision_links):
			print(f"    [{idx+1}/{len(decision_links)}] Processing decision...")
			process_decision_page(page, url, save_dir, tracking_data)
			page.wait_for_timeout(500) # Small delay
			
	except Exception as e:
		print(f"  Error processing year {year}: {e}")


def process_tribunal(page, tribunal_url, tracking_data):
	"""Process a board/tribunal main page"""
	try:
		print(f"\nProcessing Tribunal: {tribunal_url}")
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
	print(f"Loaded tracking data: {len(tracking_data.get('processed_documents', []))} documents already processed")

	with sync_playwright() as p:
		# Determine headless mode
		system_os = platform.system()
		env_headless = os.getenv("HEADLESS")
		
		if env_headless is not None:
			is_headless = env_headless.lower() == "true"
		else:
			# Default to headless on Linux, headed on Windows
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
		
		# Add random mouse movement
		page.mouse.move(random.randint(100, 500), random.randint(100, 500))
		
		page.goto(START_URL, wait_until="load")
		
		# Initial CAPTCHA Check
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
			
			# Wait briefly to see if page auto-reloads
			print("    Waiting for page to stabilize...")
			page.wait_for_timeout(5000)
			
			# Check if we are already on the page with content
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
				except Exception as e:
					print(f"Warning: Navigation timeout after CAPTCHA, continuing anyway: {e}")
				
			page.wait_for_timeout(WAIT_MS)

		# Handle cookie consent with logging
		print("Checking for cookie banner...")
		
		try:
			if page.locator("#cookieConsentBanner").count() > 0:
				print("Cookie banner detected")
				
				# Try JavaScript click first
				try:
					page.evaluate("""
						const btn = document.getElementById('understandCookieConsent');
						if (btn) btn.click();
					""")
					print("Cookie consent clicked successfully (JavaScript)")
				except:
					# Fallback to Playwright click
					try:
						page.click("#understandCookieConsent", timeout=3000)
						print("Cookie consent clicked successfully (Playwright)")
					except:
						print("Could not dismiss cookie banner")
				
				page.wait_for_timeout(1000)
			else:
				print("No cookie banner found")
				
		except Exception as e:
			print(f"Cookie consent handling failed: {e}")
		
		page.wait_for_load_state("networkidle")
		page.wait_for_timeout(WAIT_MS)
		
		# Collect links
		print(f"\n=== Collecting {SECTION_TITLE} links ===")
		links = collect_links(page, SECTION_TITLE)
		print(f"Found {len(links)} links")
		
		# If still no links found, check for CAPTCHA again
		if len(links) == 0:
			print("⚠️  No links found, checking for CAPTCHA...")
			
			auto_solved = False
			if is_captcha_page(page):
				auto_solved = solve_captcha_automatically(page)
			
			if not auto_solved and is_captcha_page(page):
				print("    Please solve the CAPTCHA in the browser window...")
				while is_captcha_page(page):
					page.wait_for_timeout(5000)
				print("✅ CAPTCHA solved! Continuing...")
				auto_solved = True
			
			if auto_solved or len(links) == 0:
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
				links = collect_links(page, SECTION_TITLE)
				print(f"Found {len(links)} links after retry")
		
		for i, href in enumerate(links, 1):
			print(f"Visiting link {i}/{len(links)}: {href}")
			url = f"{BASE_URL}{href}"
			process_tribunal(page, url, tracking_data)
			
		browser.close()


if __name__ == "__main__":
	main()
