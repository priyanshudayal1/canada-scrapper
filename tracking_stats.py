import json
import os
from datetime import datetime
from collections import defaultdict

def load_tracking_file(filepath):
    """Load tracking JSON file"""
    if not os.path.exists(filepath):
        print(f"⚠️  File not found: {filepath}")
        return None
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Error loading {filepath}: {e}")
        return None


def analyze_tracking_data(data, name):
    """Analyze tracking data and print statistics"""
    if not data:
        print(f"\n📊 {name}: No data available\n")
        return
    
    processed = data.get('processed_documents', [])
    total_processed = len(processed)
    
    # Count by date
    dates = defaultdict(int)
    for doc in processed:
        date = doc.get('downloaded_at', '').split(' ')[0] if doc.get('downloaded_at') else 'Unknown'
        dates[date] += 1
    
    # Get first and last download
    if processed:
        first_doc = min(processed, key=lambda x: x.get('downloaded_at', ''))
        last_doc = max(processed, key=lambda x: x.get('downloaded_at', ''))
        first_date = first_doc.get('downloaded_at', 'Unknown')
        last_date = last_doc.get('downloaded_at', 'Unknown')
    else:
        first_date = last_date = 'N/A'
    
    # Print statistics
    print(f"\n{'='*60}")
    print(f"📊 {name} Statistics")
    print(f"{'='*60}")
    print(f"✅ Total Processed: {total_processed}")
    print(f"📅 First Download: {first_date}")
    print(f"📅 Last Download: {last_date}")
    
    if dates:
        print(f"\n📈 Downloads by Date:")
        for date in sorted(dates.keys()):
            print(f"   {date}: {dates[date]} documents")
    
    # Check for potential duplicates
    urls = [doc.get('url') for doc in processed if doc.get('url')]
    s3_keys = [doc.get('s3_key') for doc in processed if doc.get('s3_key')]
    
    duplicate_urls = len(urls) - len(set(urls))
    duplicate_s3_keys = len(s3_keys) - len(set(s3_keys))
    
    if duplicate_urls > 0 or duplicate_s3_keys > 0:
        print(f"\n⚠️  Potential Issues:")
        if duplicate_urls > 0:
            print(f"   Duplicate URLs: {duplicate_urls}")
        if duplicate_s3_keys > 0:
            print(f"   Duplicate S3 Keys: {duplicate_s3_keys}")
    
    print(f"{'='*60}\n")
    
    return total_processed


def main():
    print("\n" + "="*60)
    print("📈 CanLII Scraper Tracking Analysis")
    print("="*60)
    
    # Load tracking files
    courts_data = load_tracking_file("court_tracking.json")
    boards_data = load_tracking_file("boards_tracking.json")
    
    # Analyze each tracking file
    courts_total = analyze_tracking_data(courts_data, "Courts")
    boards_total = analyze_tracking_data(boards_data, "Boards & Tribunals")
    
    # Combined summary
    if courts_total is not None or boards_total is not None:
        total = (courts_total or 0) + (boards_total or 0)
        print(f"\n{'='*60}")
        print(f"🎯 Overall Summary")
        print(f"{'='*60}")
        print(f"Courts: {courts_total or 0} documents")
        print(f"Boards & Tribunals: {boards_total or 0} documents")
        print(f"Total: {total} documents")
        print(f"{'='*60}\n")
    
    # Check for files in S3 vs local
    print("\n💡 Note: This script only counts processed documents from tracking files.")
    print("   To verify S3 uploads and identify errors, check the scraper logs.")


if __name__ == "__main__":
    main()
