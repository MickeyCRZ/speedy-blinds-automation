import os
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
creds_path = "/Users/michael/Automate/credentials.json"
creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
service = build("drive", "v3", credentials=creds)

sheets = {
    "VT Thomas": "1-SYmL5vG1P5BAeq1YAEbZeN_H6e5sjn_",
    "Alen":      "1YW9uJjL7ysCUChNfK8FN3EqwFXunABS0",
    "Phil":      "1ptuQdpk6RwSB7FLZKtRsSuZuk8IcaTlx",
    "Han":       "1TmlK-Obhe4G8kXYN12P7bIDnxaytBWf7"
}

for name, file_id in sheets.items():
    try:
        file = service.files().get(fileId=file_id, fields="id, name, mimeType").execute()
        print(f"{name}: {file['name']} ({file['mimeType']})")
    except Exception as e:
        print(f"{name}: ERROR - {e}")

