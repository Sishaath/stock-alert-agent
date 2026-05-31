#!/usr/bin/env python3
"""
generate_token.py — Manual Zerodha Kite OAuth Token Generator.

This script opens the Zerodha login page in your default browser,
allows you to log in, and then prompts you to paste the redirected URL
to extract the request token and exchange it for an access token.

Saves the token to your local DATA_DIR/kite_token.json file automatically.
"""

import os
import json
import webbrowser
from urllib.parse import parse_qs, urlparse
from datetime import datetime
from zoneinfo import ZoneInfo
from kiteconnect import KiteConnect
from dotenv import load_dotenv

# Load env variables
load_dotenv()

from config import DATA_DIR, KITE_API_KEY, KITE_API_SECRET

IST = ZoneInfo("Asia/Kolkata")
TOKEN_FILE = os.path.join(DATA_DIR, "kite_token.json")

def main():
    print("=" * 60)
    print("  Kite Connect OAuth Token Generator  ")
    print("=" * 60)

    api_key = KITE_API_KEY or input("Enter your Kite API Key: ").strip()
    api_secret = KITE_API_SECRET or input("Enter your Kite API Secret: ").strip()

    if not api_key or not api_secret:
        print("Error: API Key and API Secret are required.")
        return

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    print("\n[Step 1] Opening Zerodha Login Page in your browser...")
    print(f"URL: {login_url}")
    webbrowser.open(login_url)

    print("\n[Step 2] Complete the authentication:")
    print("1. Log in with your Zerodha credentials & TOTP on the browser page.")
    print("2. Once successfully logged in, you will be redirected to your app's redirect URL.")
    print("3. That page might fail to load (e.g. 'Unable to connect' or 127.0.0.1) — this is NORMAL.")
    print("4. Copy the ENTIRE URL from your browser's address bar.")

    redirect_url = input("\n[Step 3] Paste the redirected URL here:\n> ").strip()

    if not redirect_url:
        print("Error: URL cannot be empty.")
        return

    try:
        parsed_url = urlparse(redirect_url)
        query_params = parse_qs(parsed_url.query)
        request_token = query_params.get("request_token", [None])[0]

        # Fallback: maybe they just pasted the request token itself (32 chars)
        if not request_token and len(redirect_url) == 32:
            request_token = redirect_url

        if not request_token:
            raise Exception("Could not find 'request_token' in the pasted URL.")

        print(f"\nExtracted request_token: {request_token}")
        print("Exchanging for access token...")

        session = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session["access_token"]
        public_token = session.get("public_token", "")

        # Save to the JSON token file
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "access_token": access_token,
                "public_token": public_token,
                "saved_at": datetime.now(IST).isoformat(),
            }, f, indent=2)

        print("\n" + "=" * 60)
        print("  SUCCESS: Kite access token generated and saved!  ")
        print("=" * 60)
        print(f"Token saved to: {TOKEN_FILE}")
        print("You are ready to run: python main.py")
        print("=" * 60)

    except Exception as e:
        print(f"\n[ERROR] Token generation failed: {e}")
        print("Please ensure your API secret matches, and that the redirected URL is copied entirely.")

if __name__ == "__main__":
    main()
