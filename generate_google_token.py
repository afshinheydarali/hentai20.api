import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    client_file = os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json")
    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    creds = flow.run_local_server(port=0)
    token = json.loads(creds.to_json())

    with open("token.json", "w", encoding="utf-8") as f:
        json.dump(token, f, ensure_ascii=False)

    print("token.json created.")
    print(json.dumps(token, separators=(",", ":")))


if __name__ == "__main__":
    main()
