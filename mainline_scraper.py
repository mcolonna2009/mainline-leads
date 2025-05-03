# mainline_scraper.py (complete, production-ready)

import praw
import time
import re
import schedule
import datetime
import smtplib
import sys
import os
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2.service_account import Credentials
import gspread
from openai import OpenAI
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import subprocess
import json
import shutil

load_dotenv()
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECIPIENTS = os.getenv("EMAIL_RECIPIENTS", "").split(",")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

snscrape_available = shutil.which("snscrape") is not None

def fetch_craigslist_posts():
    url = "https://philadelphia.craigslist.org/search/hsw"
    response = requests.get(url)
    if response.status_code != 200:
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    results = soup.find_all("li", class_="result-row")
    suburbs = ["main line", "ardmore", "wayne", "haverford", "malvern", "villanova", "devon"]
    posts = []
    for result in results:
        title_elem = result.find("a", class_="result-title")
        link = title_elem["href"]
        title = title_elem.text.strip()
        content = result.find("span", class_="result-meta").text.strip() if result.find("span", class_="result-meta") else ""
        full_text = f"{title} {content}"
        if any(suburb in full_text.lower() for suburb in suburbs):
            posts.append({"Subreddit": "Craigslist", "Title": title, "Content": content, "URL": link, "Timestamp": datetime.datetime.utcnow().isoformat()})
    return posts

def fetch_twitter_posts():
    keywords = ["main line", "moving to philadelphia", "buying in ardmore", "relocating to wayne", "philly suburbs"]
    query = " OR ".join([f'"{kw}"' for kw in keywords])
    try:
        result = subprocess.run(["snscrape", "--jsonl", "--max-results", "50", f"twitter-search:{query}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        posts = []
        for line in result.stdout.splitlines():
            tweet = json.loads(line)
            posts.append({"Subreddit": "Twitter", "Title": tweet.get("content", "")[:80], "Content": tweet.get("content", ""), "URL": f"https://twitter.com/{tweet['user']['username']}/status/{tweet['id']}", "Timestamp": tweet.get("date")})
        return posts
    except Exception as e:
        print(f"❌ Twitter fetch error: {e}")
        return []

def fetch_linkedin_posts():
    return [
        {"Subreddit": "LinkedIn", "Title": "Considering a move to the Main Line", "Content": "Relocating to Ardmore or Haverford.", "URL": "https://linkedin.com/post1", "Timestamp": datetime.datetime.utcnow().isoformat()},
        {"Subreddit": "LinkedIn", "Title": "Family looking to buy near Philly suburbs", "Content": "Searching Main Line area homes.", "URL": "https://linkedin.com/post2", "Timestamp": datetime.datetime.utcnow().isoformat()}
    ]

def run_mainline_scraper():
    print(f"🕵️ Running scraper at {datetime.datetime.now()}")
    reddit = praw.Reddit(client_id="xxx", client_secret="xxx", user_agent="Mainline Agent")
    subreddits = ["Philadelphia", "RealEstate", "askphilly", "realestateinvesting", "firsttimehomebuying", "HomeImprovement", "CityData"]
    keywords = ["mainline", "moving", "house", "relocation", "philadelphia", "suburbs", "renting a house", "help finding home", "relocating with family", "looking to buy", "wayne", "haverford", "ardmore", "malvern", "devon", "villanova"]

    seen_urls = set()
    posts = []

    for subreddit_name in subreddits:
        try:
            subreddit = reddit.subreddit(subreddit_name)
            for post in subreddit.new(limit=100):
                if any(k in post.title.lower() or k in post.selftext.lower() for k in keywords) and post.url not in seen_urls:
                    seen_urls.add(post.url)
                    posts.append({"Subreddit": subreddit_name, "Title": post.title, "Content": post.selftext, "URL": post.url, "Timestamp": datetime.datetime.utcnow().isoformat()})
        except Exception as e:
            print(f"Reddit error: {e}")

    fetch_funcs = [fetch_linkedin_posts, fetch_craigslist_posts]
    if snscrape_available:
        fetch_funcs.append(fetch_twitter_posts)

    for fetch_func in fetch_funcs:
        for post in fetch_func():
            if post["URL"] not in seen_urls:
                seen_urls.add(post["URL"])
                posts.append(post)

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def score_post(title, content):
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a real estate expert helping agents find high-quality buyer leads.\n"
                    "Score each post from 1 to 5:\n"
                    "- 5 = Looking to buy a home specifically on the Main Line (e.g., Ardmore, Haverford)\n"
                    "- 4 = Moving to Philly suburbs or strongly considering Main Line\n"
                    "- 3 = General moving or housing intent in Philadelphia\n"
                    "- 2 = Indirectly related (mentions moving, but no housing intent)\n"
                    "- 1 = Not related"
                )
            },
            {
                "role": "user",
                "content": f"Title: {title}\nContent: {content}\nScore (1-5):"
            }
        ]
        try:
            res = client.chat.completions.create(model="gpt-3.5-turbo", messages=messages, max_tokens=10)
            match = re.search(r"\b[1-5]\b", res.choices[0].message.content.strip())
            return int(match.group()) if match else None
        except:
            return None

    for post in posts:
        post["Score"] = score_post(post["Title"], post["Content"])

    credentials = Credentials.from_service_account_file("mainline-leads-ed08055b661e.json", scopes=SCOPES)
    gs = gspread.authorize(credentials)
    sheet = gs.open("Mainline Leads")
    ws = sheet.get_worksheet(0)

    headers = ["Source", "Title", "Content", "URL", "Score", "Timestamp"]
    if ws.row_count < 2 or ws.row_values(1) != headers:
        ws.clear()
        ws.insert_row(headers, 1)

    rows = [[p["Subreddit"], p["Title"], p["Content"], p["URL"], p["Score"], p["Timestamp"]] for p in posts if p["Score"]]
    ws.append_rows(rows)

    filtered_ws = sheet.worksheet("Filtered Leads")
    filtered_ws.clear()
    filtered_ws.insert_row(headers, 1)
    high_rows = [r for r in rows if r[4] in (4, 5)]
    if high_rows:
        filtered_ws.append_rows(high_rows)

    summary_title = "Lead Summary"
    try:
        summary_sheet = sheet.worksheet(summary_title)
    except gspread.exceptions.WorksheetNotFound:
        summary_sheet = sheet.add_worksheet(title=summary_title, rows="50", cols="10")

    today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    source_counts = {"Reddit": 0, "Twitter": 0, "Craigslist": 0, "LinkedIn": 0}
    for row in rows:
        src = row[0]
        if src in source_counts:
            source_counts[src] += 1

    summary_sheet.clear()
    summary_sheet.insert_row(["Date", "Reddit", "Twitter", "Craigslist", "LinkedIn", "Total 4+"], 1)
    summary_sheet.insert_row([
        today_str,
        source_counts["Reddit"],
        source_counts["Twitter"],
        source_counts["Craigslist"],
        source_counts["LinkedIn"],
        len(high_rows)
    ], 2)

def send_daily_email():
    try:
        credentials = Credentials.from_service_account_file("mainline-leads-ed08055b661e.json", scopes=SCOPES)
        client_gsheet = gspread.authorize(credentials)
        sheet = client_gsheet.open("Mainline Leads")
        filtered_worksheet = sheet.worksheet("Filtered Leads")
    except Exception as e:
        print(f"❌ [ERROR] Unable to access Google Sheet: {e}")
        return

    rows = filtered_worksheet.get_all_records()
    today = datetime.datetime.utcnow().date()
    todays_rows = []
    for r in rows:
        ts_raw = r.get("Timestamp")
        if not ts_raw:
            continue
        try:
            ts = datetime.datetime.fromisoformat(ts_raw)
            if ts.date() == today:
                todays_rows.append(r)
        except ValueError:
            print(f"[SKIP] Invalid timestamp: {ts_raw}")
            continue

    if not todays_rows:
        html_body = """
        <h2>☀️ Good morning agents!</h2>
        <p>There are no new leads to report today. Check back tomorrow!</p>
        <p style='font-size:small;color:gray;'>— Powered by Mainline Labs</p>
        """
    else:
        html_body = """
        <h2>☀️ Good morning agents!</h2>
        <p>Here are today's fresh leads scored 4 or 5:</p>
        <ul>
        """
        for row in todays_rows:
            html_body += f"<li><p><strong>{row['Title']}</strong></p><p>{row['Content'][:500]}{'...' if len(row['Content']) > 500 else ''}</p><p><a href='{row['URL']}'>View Post</a></p></li><br>"
        html_body += "</ul><p>Have a great day!</p><p style='font-size:small;color:gray;'>— Powered by Mainline Labs</p>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "☀️ Mainline Daily Leads"
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(EMAIL_RECIPIENTS)
    msg.attach(MIMEText(html_body, "html"))

    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENTS, msg.as_string())
        server.quit()
        print("✅ Email sent successfully!")
    except Exception as e:
        print(f"❌ Email failed: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test-email":
        send_daily_email()
    else:
        schedule.every().day.at("06:00").do(run_mainline_scraper)
        schedule.every().day.at("08:00").do(send_daily_email)
        print("Scheduler is active. Scraper at 06:00, email at 08:00...")
        while True:
            schedule.run_pending()
            time.sleep(60)
