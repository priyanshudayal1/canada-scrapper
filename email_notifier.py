import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, JSON
from sqlalchemy.dialects.mysql import CHAR
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from jinja2 import Template
import os
import time
from datetime import datetime
from typing import List, Dict
from dotenv import load_dotenv
import logging
import uuid

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Email Configuration
EMAIL_CONFIG = {
    'smtp_server': os.getenv('SMTP_SERVER', 'smtp.gmail.com'),
    'smtp_port': int(os.getenv('SMTP_PORT', '587')),
    'email': os.getenv('EMAIL_ADDRESS', 'your_email@gmail.com'),
    'password': os.getenv('EMAIL_PASSWORD', 'your_app_password'),  # Use app password for Gmail
    'from_name': os.getenv('FROM_NAME', 'LexiAI Legal Research System')
}

# SQLAlchemy setup
Base = declarative_base()

TEST_MODE = False  # Set to False in production


class EmailNotifier:
    """
    Main class for handling email notifications to law firm users.
    """
        
    def get_users_from_database(self):
      """
      Fetch all users from the API endpoint.
      
      Returns:
          List[Dict]: List of user dictionaries
      """
      url = "https://www.lexiai.legal/api/law_firm/get-all-users/"
      if TEST_MODE:
          url = "http://127.0.0.1:8000/api/law_firm/get-all-users/"
          
      logger.info(f"Fetching users from API: {url}")
      
      try:
          # Headers based on your Postman configuration
          headers = {
              'Content-Type': 'application/json',
              'Cache-Control': 'no-cache',
              'User-Agent': 'PostmanRuntime/7.45.0',  # Using same as Postman
              'Accept': '*/*',
              'Accept-Encoding': 'gzip, deflate, br'
          }
          
          # Add CSRF token cookie if in test mode
          cookies = {}
          if TEST_MODE:
              # You need to get this CSRF token from your Django session
              csrf_token = "cf37f2fRya0yImKYYb1FSFhDkZjunR9D"  # Replace with actual token
              cookies['csrftoken'] = csrf_token
          
          # Make the request with headers and cookies
          response = requests.get(
              url, 
              headers=headers, 
              cookies=cookies,
              timeout=30,
              verify=True  # SSL verification
          )
          
          # logger.info(f"Response status code: {response.status_code}")
          # logger.info(f"Response headers: {dict(response.headers)}")
          
          if response.status_code == 200:
              try:
                  data = response.json()
                  users = data.get('users', [])
                  logger.info(f"Successfully fetched {len(users)} users from API")
                  return users

              except ValueError as json_error:
                  logger.error(f"JSON decode error: {json_error}")
                  logger.error(f"Response content (first 500 chars): {response.text[:500]}")
                  return []
                  
          elif response.status_code == 403:
              logger.error("❌ Access forbidden - CSRF token required")
              logger.error("You need to include a valid CSRF token from Django")
              return []
          elif response.status_code == 401:
              logger.error("❌ Authentication failed")
              return []
          elif response.status_code == 404:
              logger.error("❌ API endpoint not found")
              return []
          else:
              logger.error(f"❌ API request failed with status {response.status_code}")
              logger.error(f"Response content: {response.text}")
              return []
              
      except requests.exceptions.ConnectionError as e:
          logger.error(f"❌ Connection error: {e}")
          logger.error("Make sure Django server is running: python manage.py runserver 127.0.0.1:8000")
          return []
      except requests.exceptions.Timeout as e:
          logger.error(f"❌ Request timeout: {e}")
          return []
      except requests.exceptions.RequestException as e:
          logger.error(f"❌ Request error: {e}")
          return []
      except Exception as e:
          logger.error(f"❌ Unexpected error: {e}")
          return []

    def categorize_judgments_by_court(self, judgments_data: List[Dict]) -> Dict:
        """
        Categorize judgments by court type and extract statistics.
        
        Args:
            judgments_data: List of judgment dictionaries
            
        Returns:
            Dict: Categorized judgment data with statistics
        """
        categories = {
            'Supreme Court': [],
            'High Courts': [],
            'District Courts': [],
            'Other Courts': []
        }
        
        law_categories = {
            'Criminal Law': 0,
            'Civil Matters': 0,
            'Corporate Law': 0,
            'Family Law': 0,
            'Property Law': 0,
            'Tax Law': 0,
            'Employment Law': 0,
            'Constitutional Law': 0,
            'Other': 0
        }
        
        for judgment in judgments_data:
            court_name = judgment.get('court', '').lower()
            title = judgment.get('title', '').lower()
            
            # Categorize by court
            if 'supreme court' in court_name:
                categories['Supreme Court'].append(judgment)
            elif 'high court' in court_name:
                categories['High Courts'].append(judgment)
            elif 'district' in court_name or 'sessions' in court_name:
                categories['District Courts'].append(judgment)
            else:
                categories['Other Courts'].append(judgment)
            
            # Categorize by law type (basic keyword matching)
            if any(keyword in title for keyword in ['criminal', 'murder', 'theft', 'fraud', 'section 420', 'section 302']):
                law_categories['Criminal Law'] += 1
            elif any(keyword in title for keyword in ['civil', 'suit', 'damages', 'injunction']):
                law_categories['Civil Matters'] += 1
            elif any(keyword in title for keyword in ['company', 'corporate', 'director', 'shareholder', 'winding up']):
                law_categories['Corporate Law'] += 1
            elif any(keyword in title for keyword in ['marriage', 'divorce', 'custody', 'maintenance', 'family']):
                law_categories['Family Law'] += 1
            elif any(keyword in title for keyword in ['property', 'land', 'possession', 'title', 'lease']):
                law_categories['Property Law'] += 1
            elif any(keyword in title for keyword in ['tax', 'income tax', 'gst', 'customs', 'excise']):
                law_categories['Tax Law'] += 1
            elif any(keyword in title for keyword in ['employment', 'labour', 'worker', 'service', 'termination']):
                law_categories['Employment Law'] += 1
            elif any(keyword in title for keyword in ['constitution', 'fundamental rights', 'article', 'writ']):
                law_categories['Constitutional Law'] += 1
            else:
                law_categories['Other'] += 1
        
        # Get top 5 categories
        top_categories = sorted(law_categories.items(), key=lambda x: x[1], reverse=True)[:5]
        
        return {
            'court_categories': categories,
            'law_categories': dict(top_categories),
            'total_by_court': {k: len(v) for k, v in categories.items()},
            'top_law_categories': top_categories
        }
    
    def create_email_content(self, judgments_data: List[Dict], target_date: str, user_data: Dict) -> str:
      """
      Create HTML email content using the new template and judgment data.
      
      Args:
          judgments_data: List of judgment dictionaries
          target_date: Date string for the judgments
          user_data: Dictionary containing user information
          
      Returns:
          str: HTML email content
      """
      
      user_name = user_data.get('name', 'Valued User')
      
      # Get successful judgments
      successful_judgments = [j for j in judgments_data if j.get('download_status') == 'success']
      
      # Categorize judgments
      categorized_data = self.categorize_judgments_by_court(successful_judgments)
      
      # Process ALL judgments for template (not just featured ones)
      all_judgments_for_template = []
      available_courts = set()
      available_categories = set()
      
      for judgment in judgments_data:
          # Determine law category
          title = judgment.get('case_title', judgment.get('title', '')).lower()
          if 'criminal' in title or 'murder' in title or 'theft' in title or 'crm' in title or 'ndps' in title:
              category = 'Criminal Law'
          elif 'civil' in title or 'suit' in title or 'wpa' in title or 'wplrt' in title:
              category = 'Civil Matters'
          elif 'company' in title or 'corporate' in title:
              category = 'Corporate Law'
          elif 'property' in title or 'land' in title:
              category = 'Property Law'
          elif 'tax' in title:
              category = 'Tax Law'
          elif 'cam' in title or 'fmat' in title:
              category = 'Compensation Law'
          else:
              category = 'General Law'
          
          court_name = judgment.get('court', 'Unknown Court')
          
          # Add to sets for filter options
          available_courts.add(court_name)
          available_categories.add(category)
          
          all_judgments_for_template.append({
              'title': judgment.get('case_title', judgment.get('title', 'Unknown Case')),
              'court': court_name,
              'category': category,
              'date': target_date,
              'link': judgment.get('link', '#'),
              'is_new': True,
              'status': judgment.get('download_status', 'unknown')
          })
      
      # Get featured judgments (first 5 successful ones)
      featured_judgments = successful_judgments[:5] if successful_judgments else []
      featured_for_template = []
      
      for judgment in featured_judgments:
          title = judgment.get('case_title', judgment.get('title', '')).lower()
          if 'criminal' in title or 'murder' in title or 'theft' in title or 'crm' in title:
              category = 'Criminal Law'
          elif 'civil' in title or 'suit' in title or 'wpa' in title:
              category = 'Civil Matters'
          elif 'company' in title or 'corporate' in title:
              category = 'Corporate Law'
          elif 'property' in title or 'land' in title:
              category = 'Property Law'
          elif 'tax' in title:
              category = 'Tax Law'
          else:
              category = 'General Law'
          
          featured_for_template.append({
              'title': judgment.get('case_title', judgment.get('title', 'Unknown Case')),
              'court': judgment.get('court', 'Unknown Court'),
              'category': category,
              'date': target_date,
              'link': judgment.get('link', '#'),
              'is_new': True
          })
      
      # Create law category summary for template
      law_categories_text = []
      for category, count in categorized_data['top_law_categories']:
          if count > 0:
              law_categories_text.append(f'<span class="highlight">{category} ({count})</span>')
      
      law_categories_summary = ' • '.join(law_categories_text[:5]) if law_categories_text else 'No categorized judgments today'
      
      # Updated HTML template based on the provided template
      email_template = """
      <!DOCTYPE html>
  <html lang="en">
  <head>
    <meta charset="UTF-8"/>
    <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
    <title>Daily Legal Insights - {{ total_judgments }} Fresh Judgments</title>
    <style>
      body {
        margin: 0;
        padding: 0;
        background: linear-gradient(135deg, #f4f8fb 0%, #e8f2f6 100%);
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        color: #333;
      }

      /* Signature Section */
      .signature-section {
        background: #ffffff;
        border-radius: 12px;
        padding: 30px 25px;
        margin: 30px 0 20px;
        border-top: 1px solid #e5e9ec;
      }

      .signature-content {
        max-width: 400px;
      }

      .signature-text {
        font-size: 16px;
        color: #333;
        margin: 0 0 15px 0;
        font-weight: 500;
      }

      .signature-details {
        display: flex;
        align-items: flex-start;
        gap: 15px;
      }

      .signature-image {
        width: 120px;
        height: auto;
        max-height: 60px;
        object-fit: contain;
      }

      .signature-info {
        flex: 1;
      }

      .signature-name {
        font-size: 18px;
        font-weight: 700;
        color: #000F24;
        margin: 0 0 5px 0;
      }

      .signature-person {
        font-size: 16px;
        color: #BF8F4C;
        font-weight: 600;
        margin: 0;
      }

      @media only screen and (max-width: 600px) {
        .signature-section {
          padding: 20px 15px;
        }
        
        .signature-details {
          flex-direction: column;
          align-items: flex-start;
          gap: 10px;
        }
        
        .signature-image {
          width: 100px;
          max-height: 50px;
        }
      }

      
      .container {
        max-width: 650px;
        margin: auto;
        background: #ffffff;
        border-radius: 16px;
        overflow: hidden;
        box-shadow: 0 10px 30px rgba(0,0,0,0.12);
        margin-top: 20px;
        margin-bottom: 20px;
      }
      .header {
        background: linear-gradient(135deg, #000F24 0%, #1a2332 100%);
        color: #fff;
        text-align: center;
        padding: 40px 20px;
        position: relative;
        overflow: hidden;
      }
      .header::before {
        content: '';
        position: absolute;
        top: -50%;
        left: -50%;
        width: 200%;
        height: 200%;
        background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 70%);
        animation: pulse 4s ease-in-out infinite;
      }
      @keyframes pulse {
        0%, 100% { transform: scale(1); opacity: 0.3; }
        50% { transform: scale(1.1); opacity: 0.5; }
      }
      .header h1 {
        margin: 15px 0 10px;
        font-size: 28px;
        font-weight: 700;
        position: relative;
        z-index: 2;
      }
      .header p {
        margin: 0;
        font-size: 16px;
        opacity: 0.95;
        position: relative;
        z-index: 2;
      }
      .logo-wrapper {
        display: inline-block;
        padding: 4px;
        border-radius: 50%;
        background: linear-gradient(135deg, #BF8F4C, #DEA63B, #F7BE45);
        position: relative;
        z-index: 2;
      }
      .logo-circle {
        width: 80px;
        height: 80px;
        border-radius: 50%;
        background: #fff;
        object-fit: cover;
        display: block;
        font-size: 40px;
        line-height: 80px;
        text-align: center;
      }
      .stats-banner {
        background: linear-gradient(90deg, #BF8F4C 0%, #DEA63B 50%, #F7BE45 100%);
        padding: 20px;
        text-align: center;
        color: #fff;
      }
      .stats-number {
        font-size: 48px;
        font-weight: 800;
        margin: 0;
        text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
      }
      .stats-text {
        font-size: 18px;
        margin: 5px 0 0;
        font-weight: 500;
      }
      .content {
        padding: 30px 25px;
      }
      .intro-text {
        font-size: 18px;
        font-weight: 600;
        color: #000F24;
        margin-bottom: 15px;
        text-align: center;
      }
      .description {
        font-size: 16px;
        margin-bottom: 30px;
        color: #555;
        text-align: center;
        line-height: 1.6;
      }
      .section {
        margin-bottom: 30px;
      }
      .section-title {
        font-size: 20px;
        font-weight: 700;
        color: #000F24;
        margin-bottom: 20px;
        text-align: center;
        position: relative;
      }
      .section-title::after {
        content: '';
        position: absolute;
        bottom: -8px;
        left: 50%;
        transform: translateX(-50%);
        width: 60px;
        height: 3px;
        background: linear-gradient(90deg, #BF8F4C, #DEA63B);
        border-radius: 2px;
      }

      /* Judgments Display */
      .judgments-list {
        background: #f8fafb;
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 25px;
      }
      .judgment-item {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 12px 0;
        border-bottom: 1px solid #e5e9ec;
        transition: all 0.2s ease;
      }
      .judgment-item:last-child {
        border-bottom: none;
      }
      .judgment-item:hover {
        background: rgba(191, 143, 76, 0.05);
      }
      .judgment-info {
        flex: 1;
      }
      .judgment-title {
        font-size: 15px;
        font-weight: 600;
        color: #000F24;
        margin-bottom: 4px;
        line-height: 1.3;
      }
      .judgment-meta {
        font-size: 12px;
        color: #777;
      }
      .judgment-actions {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      /* sent to last */
      .judgment-link {
        margin-left: auto;
        color: #fff;
        text-decoration: none;
        padding: 6px 12px;
        border-radius: 20px;
        font-size: 12px;
        font-weight: 500;
        transition: all 0.3s ease;
      }
      .judgment-link:hover {
        color: #649b9a;
      }

      .cta-section {
        background: linear-gradient(135deg, #f8fafb 0%, #e8f2f6 100%);
        border-radius: 12px;
        padding: 25px;
        text-align: center;
        margin-bottom: 20px;
      }
      .cta-button {
        display: inline-block;
        background: linear-gradient(135deg, #BF8F4C 0%, #DEA63B 100%);
        color: #fff;
        text-decoration: none;
        padding: 15px 30px;
        border-radius: 30px;
        font-size: 16px;
        font-weight: 600;
        margin-top: 15px;
        transition: all 0.3s ease;
        box-shadow: 0 4px 15px rgba(191, 143, 76, 0.3);
      }
      .cta-button:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(191, 143, 76, 0.4);
      }
      .footer {
        background: #f7f9fa;
        padding: 20px;
        text-align: center;
        font-size: 13px;
        color: #888;
        border-top: 1px solid #e5e9ec;
      }
      .highlight {
        color: #BF8F4C;
        font-weight: 600;
      }
      .badge {
        display: inline-block;
        background: #000F24;
        color: #fff;
        border-radius: 20px;
        padding: 6px 12px;
        font-size: 12px;
        font-weight: 500;
        margin-left: 8px;
      }
      .no-judgments {
        text-align: center;
        padding: 40px 20px;
        color: #666;
      }
      .no-judgments h3 {
        color: #000F24;
        margin-bottom: 15px;
      }

      @media only screen and (max-width: 600px) {
        .container {
          margin: 10px;
          border-radius: 12px;
        }
        .header h1 { font-size: 24px; }
        .stats-number { font-size: 36px; }
        .content { padding: 25px 20px; }
        .cta-section { padding: 20px 15px; }
        .judgment-item {
          flex-direction: column;
          align-items: flex-start;
          gap: 10px;
        }
        .judgment-actions {
          width: 100%;
          justify-content: flex-end;
        }
      }
    </style>
  </head>
  <body>
    <div class="container">
      <!-- Header -->
      <div class="header">
        <div class="logo-wrapper">
          <div class="logo-circle">⚖️</div>
        </div>
        <h1>🏛️ Daily Legal Intelligence</h1>
        <p>Fresh judgments delivered to your inbox • {{ target_date }}</p>
      </div>
      
      <!-- Stats Banner -->
      <div class="stats-banner">
        <div class="stats-number">{{ total_judgments }}+</div>
        <div class="stats-text">New Judgments Available Today</div>
      </div>
      
      <!-- Content -->
      <div class="content">
        <p class="intro-text">Hello {{ user_name }}, your legal edge awaits! ⚡</p>
        
        {% if total_judgments > 0 %}
        <p class="description">
          We've processed and analyzed <span class="highlight">{{ total_judgments }} fresh judgments</span> from courts across India today. 
          <span class="highlight">{{ successful_judgments }} judgments</span> are now ready for your legal research and analysis.
        </p>

        <!-- All Judgments Section -->
        <div class="section">
          <div class="section-title">📋 All Today's Judgments ({{ total_judgments }})</div>
          <div class="judgments-list">
            {% for judgment in all_judgments %}
            <div class="judgment-item">
              <div class="judgment-info">
                <div class="judgment-title">{{ judgment.title }}</div>
                <div class="judgment-meta">{{ judgment.court }} • {{ judgment.category }} • {{ judgment.date }}</div>
              </div>
            </div>
            {% endfor %}
          </div>
        </div>
        
        {% else %}
        <!-- No Judgments Section -->
        <div class="no-judgments">
          <h3>📋 No New Judgments Today</h3>
          <p>No new judgments were processed for {{ target_date }}. This could indicate:</p>
          <ul style="text-align: left; display: inline-block; margin: 15px 0;">
            <li>Courts did not publish new judgments on this date</li>
            <li>All available judgments were previously processed</li>
            <li>Temporary technical issues with court databases</li>
          </ul>
          <p><strong>Stay tuned!</strong> We'll continue monitoring and notify you as soon as new judgments become available.</p>
        </div>
        {% endif %}

        <!-- CTA Section -->
        <div class="cta-section">
          <h3 style="color: #000F24; margin-top: 0;">🚀 Ready to Dive Deeper?</h3>
          <p>Access your LeXi AI dashboard for AI-powered legal research, case analysis, and intelligent search across all judgments.</p>
          <a href="https://www.lexiai.legal" class="cta-button">Open LeXi AI </a>
        </div>
        
        <p class="description">
          💡 <strong>Pro Tip:</strong> Use our AI-powered LeXi AI Legal Assistant to streamline your research process. Get instant access to relevant case law, statutes, and legal insights tailored to your needs. Create drafts within seconds.
        </p>
      </div>
      
      <div class="signature-section">
        <div class="signature-content">
          <p class="signature-text">Best regards,</p>
          <div class="signature-details">
            <!-- <img src="sign.png" alt="Signature" class="signature-image"> -->
            <div class="signature-info">
              <p class="signature-name">Team LeXi AI</p>
              <p class="signature-person">Onkar Rana</p>
              <p class="signature-person">Founder & CEO</p>
            </div>
          </div>
        </div>
      </div>      
      
      <!-- Footer -->
      <div class="footer">
        <p>© {{ current_year }} LeXi AI. Empowering legal professionals with AI-driven insights.</p>
        <p>This automated report contains judgments sourced from official government eCourts platforms.</p>
        <p style="margin-top: 10px; font-size: 12px; color: #aaa;">
          Sent to {{ user_email }} • For support, contact our team
        </p>
      </div>
    </div>
  </body>
  </html>
      """
      
      template = Template(email_template)
      
      return template.render(
          user_name=user_name,
          target_date=target_date,
          total_judgments=len(judgments_data),
          successful_judgments=len(successful_judgments),
          featured_judgments=featured_for_template,
          all_judgments=all_judgments_for_template,  # 🔥 THIS WAS MISSING!
          available_courts=sorted(list(available_courts)),
          available_categories=sorted(list(available_categories)),
          law_categories_summary=law_categories_summary,
          court_distribution=categorized_data['total_by_court'],
          current_year=datetime.now().year,
          user_email=user_data.get('email', '[USER_EMAIL]')
      )
    
    
    def send_email_to_user(self, user_email: str, user_data: Dict, subject: str, html_content: str) -> bool:
        """
        Send email to a single user.
        
        Args:
            user_email: Recipient email address
            user_data: User data dictionary
            subject: Email subject
            html_content: HTML email content
            
        Returns:
            bool: True if email sent successfully, False otherwise
        """
        try:
            #  use these instead of the env file
            SMTP_SERVER='smtp.gmail.com'
            SMTP_PORT=587
            EMAIL_ADDRESS='astutelexservicado@gmail.com'
            EMAIL_PASSWORD='qvex kdrh owoj bdjw'
            FROM_NAME='ceo@lexiai.legal'
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f"{FROM_NAME} <{EMAIL_ADDRESS}>"
            msg['To'] = user_email
            
            html_part = MIMEText(html_content, 'html')
            msg.attach(html_part)

            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                server.send_message(msg)
                
            logger.info(f"✅ Email sent successfully to {user_email} ({user_data.get('company_name', 'Unknown Company')})")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to send email to {user_email}: {e}")
            return False
    
    def send_mail_to_all_users(self, all_judgments_metadata: List[Dict], target_date: str) -> Dict:
        """
        Main function to send emails to all users in database.
        
        Args:
            all_judgments_metadata: List of judgment metadata from scraper
            target_date: Date string for the judgments
            
        Returns:
            Dict: Summary of email sending results
        """
        logger.info("=" * 60)
        logger.info("STARTING EMAIL NOTIFICATION PROCESS")
        logger.info("=" * 60)
        
        users = self.get_users_from_database()
        if not users:
            logger.warning("No active users with verified emails found in database. Skipping email notifications.")
            return {
                'total_users': 0,
                'emails_sent': 0,
                'emails_failed': 0,
                'success_rate': 0,
                'judgments_included': 0
            }
        # try:    
        #   users.append({
        #     "name": "Onkar Rana",
        #     "email": "onkarrana70@gmail.com",
        #     "company_name": "LeXi AI"
        #   })
        # except Exception as e:
        #   logger.error(f"Error adding test user: {e}")
        
        # Prepare judgment data for email
        judgment_data = []
        for judgment in all_judgments_metadata:
            gov_link = ""
            if judgment.get('modal_pdf_url'):
                gov_link = judgment.get('modal_pdf_url')
            elif judgment.get('pdf_path'):
                gov_link = f"https://judgments.ecourts.gov.in{judgment.get('pdf_path')}"
            
            judgment_data.append({
                'title': judgment.get('case_title', 'Unknown Case'),
                'cnr': judgment.get('cnr', 'N/A'),
                'court': judgment.get('court', 'Unknown Court'),
                'state': judgment.get('state', 'Other'),
                'download_status': judgment.get('download_status', 'unknown'),
                'link': gov_link,
                'error': judgment.get('error', '')
            })
        
        # Create email subject
        successful_count = len([j for j in judgment_data if j['download_status'] == 'success'])
        if successful_count > 0:
            subject = f"⚖️ Daily Legal Intelligence - {target_date} ({successful_count} New Judgments Available)"
        else:
            subject = f"⚖️ Daily Legal Intelligence - {target_date} (System Update)"
        
        # Send emails to all users
        sent_count = 0
        failed_count = 0
        
        logger.info(f"Sending personalized emails to {len(users)} active law firm users...")
        
        for user in users:
            try:
                logger.info(f"📧 Sending email to {user['email']} ({user['company_name']})...")
                
                html_content = self.create_email_content(judgment_data, target_date, user)
                
                if self.send_email_to_user(user['email'], user, subject, html_content):
                    sent_count += 1
                else:
                    failed_count += 1
                    
                time.sleep(1)  # Small delay between emails
                
            except Exception as e:
                logger.error(f"Error processing user {user['email']}: {e}")
                failed_count += 1
        
        # Email summary
        success_rate = (sent_count / len(users) * 100) if users else 0
        
        logger.info("=" * 60)
        logger.info("EMAIL NOTIFICATION SUMMARY")
        logger.info("=" * 60)
        logger.info(f"📧 Total active users: {len(users)}")
        logger.info(f"✅ Emails sent successfully: {sent_count}")
        logger.info(f"❌ Failed emails: {failed_count}")
        logger.info(f"📊 Success rate: {success_rate:.1f}%")
        logger.info(f"📑 Judgments included: {len(judgment_data)} ({successful_count} successful)")
        
        return {
            'total_users': len(users),
            'emails_sent': sent_count,
            'emails_failed': failed_count,
            'success_rate': success_rate,
            'judgments_included': len(judgment_data),
            'successful_judgments': successful_count
        }

# Convenience function for easy import
def send_judgment_notifications(all_judgments_metadata: List[Dict], target_date: str) -> Dict:
    """
    Convenience function to send judgment notifications.
    
    Args:
        all_judgments_metadata: List of judgment metadata from scraper
        target_date: Date string for the judgments
        
    Returns:
        Dict: Summary of email sending results
    """
    notifier = EmailNotifier()
    return notifier.send_mail_to_all_users(all_judgments_metadata, target_date)



def test_email_system():
    """Test function to verify email system setup and send a test email."""
    logger.info("Testing LexiAI email notification system...")
    
    notifier = EmailNotifier()
    
    # Test database connection
    users = notifier.get_users_from_database()
    
    testing_user = [
      # {
      #   "name": "Onkar Rana",
      #   "email": "onkarrana70@gmail.com",
      #   "company_name": "LeXi AI"
      # },
      {
        "name": "Divyanshu Kaintura",
        "email": "divyanshukaintura789@gmail.com",
        "company_name": "Divyanshu"
      },
    ]

    # users = testing_user

    if users:
        logger.info(f"✅ Database connection successful - found {len(users)} active users")
        logger.info(f"Sample user: {users[0]['email']} from {users[0]['company_name']}")
        
    else:
        logger.error("❌ Database connection failed or no active users found")
    
    # Test email template creation with sample data
    test_judgments = [
        {
        "case_title": "WPLRT/7/2011 of MD. SOLEMAN Vs STATE OF WEST BENGAL & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ca01b95b051411cd431effb764be7f9eb91e45289539c7d0e10d638fff516de41756274488.pdf",
        "cnr": "case_1",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPLRT_7_2011_of_MD._SOLEMAN_Vs_STATE_OF_WEST_BENGAL_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/6329/2019 of GANGABEN PATEL Vs UNION OF INDIA & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ec6aa00c4b9004a05d8e0b5bfcae76728061f8a4d972f237f1e644f9612c76591756274497.pdf",
        "cnr": "case_2",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_6329_2019_of_GANGABEN_PATEL_Vs_UNION_OF_INDIA_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/18848/2025 of AMIT GHOSH Vs THE WBSEDCL CO LTD ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/f952c03319dc6f535fd9c317eafbadc9355e83d8e1852c79f82f53e9cba425b01756274508.pdf",
        "cnr": "case_3",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_18848_2025_of_AMIT_GHOSH_Vs_THE_WBSEDCL_CO_LTD_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/806/2025 of AMAL BARMAN @ AMUL ARMAN Vs STATE OF WEST BENGAL",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/36916acfd52f5e4a242ea261d8949fe22ac771c9391abf49b8d52a0d90554a751756274518.pdf",
        "cnr": "case_4",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_806_2025_of_AMAL_BARMAN_@_AMUL_ARMAN_Vs_STATE_OF_WEST_BENGAL_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/1045/2025 of SHANTI DHIRUBHAI GOSWAMI Vs UNION OF INDIA",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e76033dce898ef5e77fd3ea3bb9971d48d1a102151203829db28f7ffa4394faf1756274527.pdf",
        "cnr": "case_5",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_1045_2025_of_SHANTI_DHIRUBHAI_GOSWAMI_Vs_UNION_OF_INDIA_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CAM/110/2025 of NATIONAL HIGHWAYS AUTHORITY OF INDIA THR.PROJECT DIRECTOR, UNIT CHANDRAPUR (EARLIER -YAVATMAL) Vs SMT. VIDYADEVI ARUNKUMAR GUPTA AND OTHERS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/7c913642bdbca7699ccee1bdbe905231c3314aab3d3269faa385ab85700a41871756274538.pdf",
        "cnr": "case_6",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CAM_110_2025_of_NATIONAL_HIGHWAYS_AUTHORITY_OF_INDIA_THR.PROJECT_DIRECTOR,_UNIT_CHANDRAPUR_(EARLIER__26082025.pdf",
        "error": ""
      },
      {
        "case_title": "FMAT (WC)/23/2024 of NATIONAL INSURANCE COMPANY LIMITED Vs ARCHANA SEN AND ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/927b78eb1175d4c1bc0351f17ad4d577340af2949dc41b7959e320fe5a0500271756274549.pdf",
        "cnr": "case_7",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\FMAT_(WC)_23_2024_of_NATIONAL_INSURANCE_COMPANY_LIMITED_Vs_ARCHANA_SEN_AND_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/3292/2025 of B.Sharafudeen (Deceased) 1.M/s.Hotel Oriental Towers Vs P.A.Abdul Jaleel",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/bbc926c70e0cd13639142c5cbfd798dec88a73d5750d661d07e04eab3d7447361756274559.pdf",
        "cnr": "case_8",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_3292_2025_of_B.Sharafudeen_(Deceased)_1.M_s.Hotel_Oriental_Towers_Vs_P.A.Abdul_Jaleel_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/2168/2025 of R.Senthil Vs Arulmigu Meenakshi Sundaraeswarar Peedam",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e6f5459a8d610885eece268c62f53803cdf2eccd8c8c5273b38d156839a6ba0a1756274570.pdf",
        "cnr": "case_9",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_2168_2025_of_R.Senthil_Vs_Arulmigu_Meenakshi_Sundaraeswarar_Peedam_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CMP/20857/2025 of Mohan, Vs Muruganandham,",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/82518074b0df6ffea071b4a015b7c2b139398b732a607e0ef3d3c4654822a5201756274580.pdf",
        "cnr": "case_10",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CMP_20857_2025_of_Mohan,_Vs_Muruganandham,_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPLRT/7/2011 of MD. SOLEMAN Vs STATE OF WEST BENGAL & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ca01b95b051411cd431effb764be7f9eb91e45289539c7d0e10d638fff516de41756274488.pdf",
        "cnr": "case_1",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPLRT_7_2011_of_MD._SOLEMAN_Vs_STATE_OF_WEST_BENGAL_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/6329/2019 of GANGABEN PATEL Vs UNION OF INDIA & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ec6aa00c4b9004a05d8e0b5bfcae76728061f8a4d972f237f1e644f9612c76591756274497.pdf",
        "cnr": "case_2",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_6329_2019_of_GANGABEN_PATEL_Vs_UNION_OF_INDIA_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/18848/2025 of AMIT GHOSH Vs THE WBSEDCL CO LTD ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/f952c03319dc6f535fd9c317eafbadc9355e83d8e1852c79f82f53e9cba425b01756274508.pdf",
        "cnr": "case_3",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_18848_2025_of_AMIT_GHOSH_Vs_THE_WBSEDCL_CO_LTD_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/806/2025 of AMAL BARMAN @ AMUL ARMAN Vs STATE OF WEST BENGAL",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/36916acfd52f5e4a242ea261d8949fe22ac771c9391abf49b8d52a0d90554a751756274518.pdf",
        "cnr": "case_4",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_806_2025_of_AMAL_BARMAN_@_AMUL_ARMAN_Vs_STATE_OF_WEST_BENGAL_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/1045/2025 of SHANTI DHIRUBHAI GOSWAMI Vs UNION OF INDIA",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e76033dce898ef5e77fd3ea3bb9971d48d1a102151203829db28f7ffa4394faf1756274527.pdf",
        "cnr": "case_5",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_1045_2025_of_SHANTI_DHIRUBHAI_GOSWAMI_Vs_UNION_OF_INDIA_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CAM/110/2025 of NATIONAL HIGHWAYS AUTHORITY OF INDIA THR.PROJECT DIRECTOR, UNIT CHANDRAPUR (EARLIER -YAVATMAL) Vs SMT. VIDYADEVI ARUNKUMAR GUPTA AND OTHERS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/7c913642bdbca7699ccee1bdbe905231c3314aab3d3269faa385ab85700a41871756274538.pdf",
        "cnr": "case_6",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CAM_110_2025_of_NATIONAL_HIGHWAYS_AUTHORITY_OF_INDIA_THR.PROJECT_DIRECTOR,_UNIT_CHANDRAPUR_(EARLIER__26082025.pdf",
        "error": ""
      },
      {
        "case_title": "FMAT (WC)/23/2024 of NATIONAL INSURANCE COMPANY LIMITED Vs ARCHANA SEN AND ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/927b78eb1175d4c1bc0351f17ad4d577340af2949dc41b7959e320fe5a0500271756274549.pdf",
        "cnr": "case_7",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\FMAT_(WC)_23_2024_of_NATIONAL_INSURANCE_COMPANY_LIMITED_Vs_ARCHANA_SEN_AND_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/3292/2025 of B.Sharafudeen (Deceased) 1.M/s.Hotel Oriental Towers Vs P.A.Abdul Jaleel",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/bbc926c70e0cd13639142c5cbfd798dec88a73d5750d661d07e04eab3d7447361756274559.pdf",
        "cnr": "case_8",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_3292_2025_of_B.Sharafudeen_(Deceased)_1.M_s.Hotel_Oriental_Towers_Vs_P.A.Abdul_Jaleel_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/2168/2025 of R.Senthil Vs Arulmigu Meenakshi Sundaraeswarar Peedam",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e6f5459a8d610885eece268c62f53803cdf2eccd8c8c5273b38d156839a6ba0a1756274570.pdf",
        "cnr": "case_9",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_2168_2025_of_R.Senthil_Vs_Arulmigu_Meenakshi_Sundaraeswarar_Peedam_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CMP/20857/2025 of Mohan, Vs Muruganandham,",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/82518074b0df6ffea071b4a015b7c2b139398b732a607e0ef3d3c4654822a5201756274580.pdf",
        "cnr": "case_10",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CMP_20857_2025_of_Mohan,_Vs_Muruganandham,_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPLRT/7/2011 of MD. SOLEMAN Vs STATE OF WEST BENGAL & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ca01b95b051411cd431effb764be7f9eb91e45289539c7d0e10d638fff516de41756274488.pdf",
        "cnr": "case_1",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPLRT_7_2011_of_MD._SOLEMAN_Vs_STATE_OF_WEST_BENGAL_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/6329/2019 of GANGABEN PATEL Vs UNION OF INDIA & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ec6aa00c4b9004a05d8e0b5bfcae76728061f8a4d972f237f1e644f9612c76591756274497.pdf",
        "cnr": "case_2",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_6329_2019_of_GANGABEN_PATEL_Vs_UNION_OF_INDIA_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/18848/2025 of AMIT GHOSH Vs THE WBSEDCL CO LTD ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/f952c03319dc6f535fd9c317eafbadc9355e83d8e1852c79f82f53e9cba425b01756274508.pdf",
        "cnr": "case_3",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_18848_2025_of_AMIT_GHOSH_Vs_THE_WBSEDCL_CO_LTD_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/806/2025 of AMAL BARMAN @ AMUL ARMAN Vs STATE OF WEST BENGAL",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/36916acfd52f5e4a242ea261d8949fe22ac771c9391abf49b8d52a0d90554a751756274518.pdf",
        "cnr": "case_4",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_806_2025_of_AMAL_BARMAN_@_AMUL_ARMAN_Vs_STATE_OF_WEST_BENGAL_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/1045/2025 of SHANTI DHIRUBHAI GOSWAMI Vs UNION OF INDIA",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e76033dce898ef5e77fd3ea3bb9971d48d1a102151203829db28f7ffa4394faf1756274527.pdf",
        "cnr": "case_5",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_1045_2025_of_SHANTI_DHIRUBHAI_GOSWAMI_Vs_UNION_OF_INDIA_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CAM/110/2025 of NATIONAL HIGHWAYS AUTHORITY OF INDIA THR.PROJECT DIRECTOR, UNIT CHANDRAPUR (EARLIER -YAVATMAL) Vs SMT. VIDYADEVI ARUNKUMAR GUPTA AND OTHERS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/7c913642bdbca7699ccee1bdbe905231c3314aab3d3269faa385ab85700a41871756274538.pdf",
        "cnr": "case_6",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CAM_110_2025_of_NATIONAL_HIGHWAYS_AUTHORITY_OF_INDIA_THR.PROJECT_DIRECTOR,_UNIT_CHANDRAPUR_(EARLIER__26082025.pdf",
        "error": ""
      },
      {
        "case_title": "FMAT (WC)/23/2024 of NATIONAL INSURANCE COMPANY LIMITED Vs ARCHANA SEN AND ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/927b78eb1175d4c1bc0351f17ad4d577340af2949dc41b7959e320fe5a0500271756274549.pdf",
        "cnr": "case_7",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\FMAT_(WC)_23_2024_of_NATIONAL_INSURANCE_COMPANY_LIMITED_Vs_ARCHANA_SEN_AND_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/3292/2025 of B.Sharafudeen (Deceased) 1.M/s.Hotel Oriental Towers Vs P.A.Abdul Jaleel",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/bbc926c70e0cd13639142c5cbfd798dec88a73d5750d661d07e04eab3d7447361756274559.pdf",
        "cnr": "case_8",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_3292_2025_of_B.Sharafudeen_(Deceased)_1.M_s.Hotel_Oriental_Towers_Vs_P.A.Abdul_Jaleel_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/2168/2025 of R.Senthil Vs Arulmigu Meenakshi Sundaraeswarar Peedam",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e6f5459a8d610885eece268c62f53803cdf2eccd8c8c5273b38d156839a6ba0a1756274570.pdf",
        "cnr": "case_9",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_2168_2025_of_R.Senthil_Vs_Arulmigu_Meenakshi_Sundaraeswarar_Peedam_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CMP/20857/2025 of Mohan, Vs Muruganandham,",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/82518074b0df6ffea071b4a015b7c2b139398b732a607e0ef3d3c4654822a5201756274580.pdf",
        "cnr": "case_10",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CMP_20857_2025_of_Mohan,_Vs_Muruganandham,_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPLRT/7/2011 of MD. SOLEMAN Vs STATE OF WEST BENGAL & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ca01b95b051411cd431effb764be7f9eb91e45289539c7d0e10d638fff516de41756274488.pdf",
        "cnr": "case_1",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPLRT_7_2011_of_MD._SOLEMAN_Vs_STATE_OF_WEST_BENGAL_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/6329/2019 of GANGABEN PATEL Vs UNION OF INDIA & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ec6aa00c4b9004a05d8e0b5bfcae76728061f8a4d972f237f1e644f9612c76591756274497.pdf",
        "cnr": "case_2",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_6329_2019_of_GANGABEN_PATEL_Vs_UNION_OF_INDIA_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/18848/2025 of AMIT GHOSH Vs THE WBSEDCL CO LTD ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/f952c03319dc6f535fd9c317eafbadc9355e83d8e1852c79f82f53e9cba425b01756274508.pdf",
        "cnr": "case_3",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_18848_2025_of_AMIT_GHOSH_Vs_THE_WBSEDCL_CO_LTD_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/806/2025 of AMAL BARMAN @ AMUL ARMAN Vs STATE OF WEST BENGAL",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/36916acfd52f5e4a242ea261d8949fe22ac771c9391abf49b8d52a0d90554a751756274518.pdf",
        "cnr": "case_4",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_806_2025_of_AMAL_BARMAN_@_AMUL_ARMAN_Vs_STATE_OF_WEST_BENGAL_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/1045/2025 of SHANTI DHIRUBHAI GOSWAMI Vs UNION OF INDIA",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e76033dce898ef5e77fd3ea3bb9971d48d1a102151203829db28f7ffa4394faf1756274527.pdf",
        "cnr": "case_5",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_1045_2025_of_SHANTI_DHIRUBHAI_GOSWAMI_Vs_UNION_OF_INDIA_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CAM/110/2025 of NATIONAL HIGHWAYS AUTHORITY OF INDIA THR.PROJECT DIRECTOR, UNIT CHANDRAPUR (EARLIER -YAVATMAL) Vs SMT. VIDYADEVI ARUNKUMAR GUPTA AND OTHERS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/7c913642bdbca7699ccee1bdbe905231c3314aab3d3269faa385ab85700a41871756274538.pdf",
        "cnr": "case_6",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CAM_110_2025_of_NATIONAL_HIGHWAYS_AUTHORITY_OF_INDIA_THR.PROJECT_DIRECTOR,_UNIT_CHANDRAPUR_(EARLIER__26082025.pdf",
        "error": ""
      },
      {
        "case_title": "FMAT (WC)/23/2024 of NATIONAL INSURANCE COMPANY LIMITED Vs ARCHANA SEN AND ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/927b78eb1175d4c1bc0351f17ad4d577340af2949dc41b7959e320fe5a0500271756274549.pdf",
        "cnr": "case_7",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\FMAT_(WC)_23_2024_of_NATIONAL_INSURANCE_COMPANY_LIMITED_Vs_ARCHANA_SEN_AND_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/3292/2025 of B.Sharafudeen (Deceased) 1.M/s.Hotel Oriental Towers Vs P.A.Abdul Jaleel",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/bbc926c70e0cd13639142c5cbfd798dec88a73d5750d661d07e04eab3d7447361756274559.pdf",
        "cnr": "case_8",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_3292_2025_of_B.Sharafudeen_(Deceased)_1.M_s.Hotel_Oriental_Towers_Vs_P.A.Abdul_Jaleel_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/2168/2025 of R.Senthil Vs Arulmigu Meenakshi Sundaraeswarar Peedam",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e6f5459a8d610885eece268c62f53803cdf2eccd8c8c5273b38d156839a6ba0a1756274570.pdf",
        "cnr": "case_9",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_2168_2025_of_R.Senthil_Vs_Arulmigu_Meenakshi_Sundaraeswarar_Peedam_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CMP/20857/2025 of Mohan, Vs Muruganandham,",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/82518074b0df6ffea071b4a015b7c2b139398b732a607e0ef3d3c4654822a5201756274580.pdf",
        "cnr": "case_10",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CMP_20857_2025_of_Mohan,_Vs_Muruganandham,_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPLRT/7/2011 of MD. SOLEMAN Vs STATE OF WEST BENGAL & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ca01b95b051411cd431effb764be7f9eb91e45289539c7d0e10d638fff516de41756274488.pdf",
        "cnr": "case_1",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPLRT_7_2011_of_MD._SOLEMAN_Vs_STATE_OF_WEST_BENGAL_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/6329/2019 of GANGABEN PATEL Vs UNION OF INDIA & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ec6aa00c4b9004a05d8e0b5bfcae76728061f8a4d972f237f1e644f9612c76591756274497.pdf",
        "cnr": "case_2",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_6329_2019_of_GANGABEN_PATEL_Vs_UNION_OF_INDIA_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/18848/2025 of AMIT GHOSH Vs THE WBSEDCL CO LTD ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/f952c03319dc6f535fd9c317eafbadc9355e83d8e1852c79f82f53e9cba425b01756274508.pdf",
        "cnr": "case_3",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_18848_2025_of_AMIT_GHOSH_Vs_THE_WBSEDCL_CO_LTD_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/806/2025 of AMAL BARMAN @ AMUL ARMAN Vs STATE OF WEST BENGAL",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/36916acfd52f5e4a242ea261d8949fe22ac771c9391abf49b8d52a0d90554a751756274518.pdf",
        "cnr": "case_4",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_806_2025_of_AMAL_BARMAN_@_AMUL_ARMAN_Vs_STATE_OF_WEST_BENGAL_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/1045/2025 of SHANTI DHIRUBHAI GOSWAMI Vs UNION OF INDIA",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e76033dce898ef5e77fd3ea3bb9971d48d1a102151203829db28f7ffa4394faf1756274527.pdf",
        "cnr": "case_5",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_1045_2025_of_SHANTI_DHIRUBHAI_GOSWAMI_Vs_UNION_OF_INDIA_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CAM/110/2025 of NATIONAL HIGHWAYS AUTHORITY OF INDIA THR.PROJECT DIRECTOR, UNIT CHANDRAPUR (EARLIER -YAVATMAL) Vs SMT. VIDYADEVI ARUNKUMAR GUPTA AND OTHERS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/7c913642bdbca7699ccee1bdbe905231c3314aab3d3269faa385ab85700a41871756274538.pdf",
        "cnr": "case_6",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CAM_110_2025_of_NATIONAL_HIGHWAYS_AUTHORITY_OF_INDIA_THR.PROJECT_DIRECTOR,_UNIT_CHANDRAPUR_(EARLIER__26082025.pdf",
        "error": ""
      },
      {
        "case_title": "FMAT (WC)/23/2024 of NATIONAL INSURANCE COMPANY LIMITED Vs ARCHANA SEN AND ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/927b78eb1175d4c1bc0351f17ad4d577340af2949dc41b7959e320fe5a0500271756274549.pdf",
        "cnr": "case_7",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\FMAT_(WC)_23_2024_of_NATIONAL_INSURANCE_COMPANY_LIMITED_Vs_ARCHANA_SEN_AND_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/3292/2025 of B.Sharafudeen (Deceased) 1.M/s.Hotel Oriental Towers Vs P.A.Abdul Jaleel",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/bbc926c70e0cd13639142c5cbfd798dec88a73d5750d661d07e04eab3d7447361756274559.pdf",
        "cnr": "case_8",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_3292_2025_of_B.Sharafudeen_(Deceased)_1.M_s.Hotel_Oriental_Towers_Vs_P.A.Abdul_Jaleel_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/2168/2025 of R.Senthil Vs Arulmigu Meenakshi Sundaraeswarar Peedam",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e6f5459a8d610885eece268c62f53803cdf2eccd8c8c5273b38d156839a6ba0a1756274570.pdf",
        "cnr": "case_9",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_2168_2025_of_R.Senthil_Vs_Arulmigu_Meenakshi_Sundaraeswarar_Peedam_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CMP/20857/2025 of Mohan, Vs Muruganandham,",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/82518074b0df6ffea071b4a015b7c2b139398b732a607e0ef3d3c4654822a5201756274580.pdf",
        "cnr": "case_10",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CMP_20857_2025_of_Mohan,_Vs_Muruganandham,_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPLRT/7/2011 of MD. SOLEMAN Vs STATE OF WEST BENGAL & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ca01b95b051411cd431effb764be7f9eb91e45289539c7d0e10d638fff516de41756274488.pdf",
        "cnr": "case_1",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPLRT_7_2011_of_MD._SOLEMAN_Vs_STATE_OF_WEST_BENGAL_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/6329/2019 of GANGABEN PATEL Vs UNION OF INDIA & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ec6aa00c4b9004a05d8e0b5bfcae76728061f8a4d972f237f1e644f9612c76591756274497.pdf",
        "cnr": "case_2",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_6329_2019_of_GANGABEN_PATEL_Vs_UNION_OF_INDIA_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/18848/2025 of AMIT GHOSH Vs THE WBSEDCL CO LTD ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/f952c03319dc6f535fd9c317eafbadc9355e83d8e1852c79f82f53e9cba425b01756274508.pdf",
        "cnr": "case_3",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_18848_2025_of_AMIT_GHOSH_Vs_THE_WBSEDCL_CO_LTD_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/806/2025 of AMAL BARMAN @ AMUL ARMAN Vs STATE OF WEST BENGAL",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/36916acfd52f5e4a242ea261d8949fe22ac771c9391abf49b8d52a0d90554a751756274518.pdf",
        "cnr": "case_4",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_806_2025_of_AMAL_BARMAN_@_AMUL_ARMAN_Vs_STATE_OF_WEST_BENGAL_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/1045/2025 of SHANTI DHIRUBHAI GOSWAMI Vs UNION OF INDIA",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e76033dce898ef5e77fd3ea3bb9971d48d1a102151203829db28f7ffa4394faf1756274527.pdf",
        "cnr": "case_5",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_1045_2025_of_SHANTI_DHIRUBHAI_GOSWAMI_Vs_UNION_OF_INDIA_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CAM/110/2025 of NATIONAL HIGHWAYS AUTHORITY OF INDIA THR.PROJECT DIRECTOR, UNIT CHANDRAPUR (EARLIER -YAVATMAL) Vs SMT. VIDYADEVI ARUNKUMAR GUPTA AND OTHERS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/7c913642bdbca7699ccee1bdbe905231c3314aab3d3269faa385ab85700a41871756274538.pdf",
        "cnr": "case_6",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CAM_110_2025_of_NATIONAL_HIGHWAYS_AUTHORITY_OF_INDIA_THR.PROJECT_DIRECTOR,_UNIT_CHANDRAPUR_(EARLIER__26082025.pdf",
        "error": ""
      },
      {
        "case_title": "FMAT (WC)/23/2024 of NATIONAL INSURANCE COMPANY LIMITED Vs ARCHANA SEN AND ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/927b78eb1175d4c1bc0351f17ad4d577340af2949dc41b7959e320fe5a0500271756274549.pdf",
        "cnr": "case_7",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\FMAT_(WC)_23_2024_of_NATIONAL_INSURANCE_COMPANY_LIMITED_Vs_ARCHANA_SEN_AND_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/3292/2025 of B.Sharafudeen (Deceased) 1.M/s.Hotel Oriental Towers Vs P.A.Abdul Jaleel",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/bbc926c70e0cd13639142c5cbfd798dec88a73d5750d661d07e04eab3d7447361756274559.pdf",
        "cnr": "case_8",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_3292_2025_of_B.Sharafudeen_(Deceased)_1.M_s.Hotel_Oriental_Towers_Vs_P.A.Abdul_Jaleel_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/2168/2025 of R.Senthil Vs Arulmigu Meenakshi Sundaraeswarar Peedam",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e6f5459a8d610885eece268c62f53803cdf2eccd8c8c5273b38d156839a6ba0a1756274570.pdf",
        "cnr": "case_9",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_2168_2025_of_R.Senthil_Vs_Arulmigu_Meenakshi_Sundaraeswarar_Peedam_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CMP/20857/2025 of Mohan, Vs Muruganandham,",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/82518074b0df6ffea071b4a015b7c2b139398b732a607e0ef3d3c4654822a5201756274580.pdf",
        "cnr": "case_10",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CMP_20857_2025_of_Mohan,_Vs_Muruganandham,_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPLRT/7/2011 of MD. SOLEMAN Vs STATE OF WEST BENGAL & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ca01b95b051411cd431effb764be7f9eb91e45289539c7d0e10d638fff516de41756274488.pdf",
        "cnr": "case_1",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPLRT_7_2011_of_MD._SOLEMAN_Vs_STATE_OF_WEST_BENGAL_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/6329/2019 of GANGABEN PATEL Vs UNION OF INDIA & ORS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/ec6aa00c4b9004a05d8e0b5bfcae76728061f8a4d972f237f1e644f9612c76591756274497.pdf",
        "cnr": "case_2",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_6329_2019_of_GANGABEN_PATEL_Vs_UNION_OF_INDIA_&_ORS_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "WPA/18848/2025 of AMIT GHOSH Vs THE WBSEDCL CO LTD ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/f952c03319dc6f535fd9c317eafbadc9355e83d8e1852c79f82f53e9cba425b01756274508.pdf",
        "cnr": "case_3",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\WPA_18848_2025_of_AMIT_GHOSH_Vs_THE_WBSEDCL_CO_LTD_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/806/2025 of AMAL BARMAN @ AMUL ARMAN Vs STATE OF WEST BENGAL",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/36916acfd52f5e4a242ea261d8949fe22ac771c9391abf49b8d52a0d90554a751756274518.pdf",
        "cnr": "case_4",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_806_2025_of_AMAL_BARMAN_@_AMUL_ARMAN_Vs_STATE_OF_WEST_BENGAL_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CRM (NDPS)/1045/2025 of SHANTI DHIRUBHAI GOSWAMI Vs UNION OF INDIA",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e76033dce898ef5e77fd3ea3bb9971d48d1a102151203829db28f7ffa4394faf1756274527.pdf",
        "cnr": "case_5",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CRM_(NDPS)_1045_2025_of_SHANTI_DHIRUBHAI_GOSWAMI_Vs_UNION_OF_INDIA_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CAM/110/2025 of NATIONAL HIGHWAYS AUTHORITY OF INDIA THR.PROJECT DIRECTOR, UNIT CHANDRAPUR (EARLIER -YAVATMAL) Vs SMT. VIDYADEVI ARUNKUMAR GUPTA AND OTHERS",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/7c913642bdbca7699ccee1bdbe905231c3314aab3d3269faa385ab85700a41871756274538.pdf",
        "cnr": "case_6",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CAM_110_2025_of_NATIONAL_HIGHWAYS_AUTHORITY_OF_INDIA_THR.PROJECT_DIRECTOR,_UNIT_CHANDRAPUR_(EARLIER__26082025.pdf",
        "error": ""
      },
      {
        "case_title": "FMAT (WC)/23/2024 of NATIONAL INSURANCE COMPANY LIMITED Vs ARCHANA SEN AND ORS.",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/927b78eb1175d4c1bc0351f17ad4d577340af2949dc41b7959e320fe5a0500271756274549.pdf",
        "cnr": "case_7",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\FMAT_(WC)_23_2024_of_NATIONAL_INSURANCE_COMPANY_LIMITED_Vs_ARCHANA_SEN_AND_ORS._26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/3292/2025 of B.Sharafudeen (Deceased) 1.M/s.Hotel Oriental Towers Vs P.A.Abdul Jaleel",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/bbc926c70e0cd13639142c5cbfd798dec88a73d5750d661d07e04eab3d7447361756274559.pdf",
        "cnr": "case_8",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_3292_2025_of_B.Sharafudeen_(Deceased)_1.M_s.Hotel_Oriental_Towers_Vs_P.A.Abdul_Jaleel_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "A/2168/2025 of R.Senthil Vs Arulmigu Meenakshi Sundaraeswarar Peedam",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/e6f5459a8d610885eece268c62f53803cdf2eccd8c8c5273b38d156839a6ba0a1756274570.pdf",
        "cnr": "case_9",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\A_2168_2025_of_R.Senthil_Vs_Arulmigu_Meenakshi_Sundaraeswarar_Peedam_26082025.pdf",
        "error": ""
      },
      {
        "case_title": "CMP/20857/2025 of Mohan, Vs Muruganandham,",
        "link": "https://judgments.ecourts.gov.in/pdfsearch/tmp/82518074b0df6ffea071b4a015b7c2b139398b732a607e0ef3d3c4654822a5201756274580.pdf",
        "cnr": "case_10",
        "court": "Unknown Court",
        "download_status": "success",
        "local_file_path": "downloaded_judgments\\CMP_20857_2025_of_Mohan,_Vs_Muruganandham,_26082025.pdf",
        "error": ""
      }
    ]
    
    # Test with the actual first user from database or use a test user
    # if users:
    #     test_user = users[0]  # Use first real user from database
    #     logger.info(f"Using real user for test: {test_user['email']}")
    # else:
    #     test_user = {
    #         'name': 'Divyanshu Kaintura',
    #         'company_name': 'Kaintura Law Firm',
    #         'email': 'divyanshukaintura789@gmail.com'
    #     }
    #     logger.info("Using mock test user data")
    
    test_user = {
        'name': 'Divyanshu Kaintura',
        'company_name': 'Kaintura Law Firm',
        'email': 'divyanshukaintura789@gmail.com'
    }
    
    # Generate email content
    today_date = datetime.now().strftime("%d-%m-%Y")
    for user in users:
      html_content = notifier.create_email_content(test_judgments, today_date, user)

      if html_content:
          logger.info("✅ Email template generation successful")
          
          # Save test email to file for review
          with open("test_lexiai_daily_judgments.html", "w", encoding="utf-8") as f:
              f.write(html_content)
          logger.info("📧 Test email saved to test_lexiai_daily_judgments.html")
          
          # 🚀 SEND ACTUAL TEST EMAIL
          logger.info("=" * 50)
          logger.info("SENDING TEST EMAIL")
          logger.info("=" * 50)
          
          subject = f"🧪 TEST: Daily Legal Intelligence - {today_date} (6 Sample Judgments)"
          
          try:
              # Send test email
              success = notifier.send_email_to_user(
                  user_email=user['email'],
                  user_data=user,
                  subject=subject,
                  html_content=html_content
              )

              
              if success:
                  logger.info("=" * 50)
                  logger.info("✅ TEST EMAIL SENT SUCCESSFULLY!")
                  logger.info("=" * 50)
                  logger.info(f"📧 Email sent to: {user['email']}")
                  logger.info(f"👤 Recipient: {user['name']}")
                  logger.info(f"🏢 Company: {user['company_name']}")
                  logger.info(f"📄 Subject: {subject}")
                  logger.info(f"📊 Test judgments included: {len(test_judgments)}")
                  logger.info(f"✅ Successful test judgments: {len([j for j in test_judgments if j['download_status'] == 'success'])}")
                  logger.info("💡 Check your email inbox to see the test email!")
                  logger.info("=" * 50)
              else:
                  logger.error("❌ TEST EMAIL FAILED TO SEND")
                  logger.error("Check your email configuration in .env file")
                  
          except Exception as e:
              logger.error(f"❌ Error sending test email: {e}")
              logger.error("Possible issues:")
              logger.error("1. Check EMAIL_ADDRESS and EMAIL_PASSWORD in .env file")
              logger.error("2. Ensure you're using an App Password for Gmail")
              logger.error("3. Check SMTP settings (server, port)")
              logger.error("4. Verify internet connection")
      else:
          logger.error("❌ Email template generation failed")
      
    # 📋 CONFIGURATION SUMMARY
    logger.info("\n" + "=" * 50)
    logger.info("CONFIGURATION SUMMARY")
    logger.info("=" * 50)
    logger.info(f"📧 SMTP Server: {EMAIL_CONFIG['smtp_server']}")
    logger.info(f"🔢 SMTP Port: {EMAIL_CONFIG['smtp_port']}")
    logger.info(f"👤 From Email: {EMAIL_CONFIG['email']}")
    logger.info(f"🏷️  From Name: {EMAIL_CONFIG['from_name']}")
    logger.info(f"👥 Active Users: {len(users) if users else 0}")
    logger.info("=" * 50)
    
    # 📝 NEXT STEPS
    logger.info("\n💡 NEXT STEPS:")
    logger.info("1. Check your email inbox for the test email")
    logger.info("2. Verify the email formatting and content")
    logger.info("3. Update .env file with correct credentials if needed")
    logger.info("4. Test with your judgment scrapers using:")
    logger.info("   from email_notifier import send_judgment_notifications")
    logger.info("   send_judgment_notifications(judgment_data, '20-08-2025')")

if __name__ == "__main__":
    test_email_system()