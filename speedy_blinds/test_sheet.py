import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
import json

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_path = "/Users/michael/Automate/credentials.json"
creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
service = build("sheets", "v4", credentials=creds)

sheet_id = "1KaauO3BEkZF3A_hW_3zgH3VE6jBME21UY0fF5GloiN4"
try:
    sheet = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    print("SUCCESS: Sheet Title:", sheet.get('properties', {}).get('title'))
    print("Tabs:")
    for s in sheet.get('sheets', []):
        print(" -", s.get('properties', {}).get('title'))
except Exception as e:
    print(f"ERROR: {e}")

