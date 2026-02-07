from playwright.sync_api import sync_playwright


def main():
	with sync_playwright() as p:
		browser = p.chromium.launch(
			headless=False,
			args=["--disable-blink-features=AutomationControlled"]
		)
		context = browser.new_context(
			user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
			viewport={"width": 1920, "height": 1080}
		)
		
		# Inject script to hide webdriver property
		context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

		page = context.new_page()
		page.goto("https://www.canlii.org/ca", wait_until="load")

		# Handle cookie consent with logging
		print("Waiting for cookie banner...")
		
		try:
			# Wait explicitly for the banner to appear
			page.wait_for_selector("#cookieConsentBanner", state="visible", timeout=10000)
			print("Cookie banner detected")
			
			# Wait for button and click
			page.wait_for_selector("#understandCookieConsent", state="visible", timeout=5000)
			print("Accept button found, attempting to click...")
			
			# Try Playwright click first
			try:
				page.click("#understandCookieConsent", timeout=3000)
				print("Cookie consent clicked successfully (Playwright)")
			except:
				print("Playwright click failed, trying JavaScript...")
				page.evaluate("document.getElementById('understandCookieConsent').click()")
				print("Cookie consent clicked successfully (JavaScript)")
			
			# Verify banner is gone
			page.wait_for_timeout(1000)
			if not page.locator("#cookieConsentBanner").is_visible():
				print("Banner dismissed successfully")
			else:
				print("Warning: Banner still visible after click")
				
		except Exception as e:
			print(f"Cookie consent handling failed: {e}")
			print("Continuing without accepting cookies...")

		browser.close()


if __name__ == "__main__":
	main()
