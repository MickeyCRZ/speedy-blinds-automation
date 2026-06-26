import os
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_path = "/Users/michael/Automate/credentials.json"
creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
service = build("sheets", "v4", credentials=creds)

base_id = "1KaauO3BEkZF3A_hW_3zgH3VE6jBME21UY0fF5GloiN4"
variations = [
    "1Kaau03BEkZF3A_hW_3zgH3VE6jBME21UY0fF5GloiN4",
    "1Kaau03BEkZF3A_hW_3zgH3VE6jBME21UY0fF5Gl0iN4",
    "1KaauO3BEkZF3A_hW_3zgH3VE6jBME21UY0fF5Gl0iN4",
    "1KaauO3BEkZF3A_hW_3zgH3VE6jBME21UYOfF5GloiN4",
    "1Kaau03BEkZF3A_hW_3zgH3VE6jBME21UYOfF5GloiN4"
]

for vid in variations:
    try:
        sheet = service.spreadsheets().get(spreadsheetId=vid).execute()
        print(f"SUCCESS with ID: {vid}")
        print("Tabs:")
        for s in sheet.get('sheets', []):
            print(" -", s.get('properties', {}).get('title'))
    except Exception as e:
        pass

print("Done testing variations.")
