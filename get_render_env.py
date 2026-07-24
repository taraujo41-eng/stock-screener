import os
import pickle

token_path = os.path.dirname(__file__)
credentials_file = os.path.join(token_path, "webull_credentials.json")
did_file = os.path.join(token_path, "did.bin")

print("=" * 60)
print("🔑 RENDER ENVIRONMENT VARIABLES GENERATOR")
print("=" * 60)

# Check credentials
token_data = None
if os.path.exists(credentials_file):
    try:
        with open(credentials_file, "rb") as f:
            token_data = pickle.load(f)
        print("✓ Found local cached Webull token file.")
    except Exception as e:
        print("✗ Failed to load Webull token file:", e)
else:
    print("✗ No local Webull token file found.")

# Check did
did_data = None
if os.path.exists(did_file):
    try:
        with open(did_file, "rb") as f:
            did_data = pickle.load(f)
        print("✓ Found local did.bin file.")
    except Exception as e:
        print("✗ Failed to load did.bin:", e)
else:
    print("✗ did.bin not found.")

if token_data and did_data:
    print("\nCopy and paste the following environment variables into your Render Service settings:")
    print("-" * 60)
    print(f"WEBULL_ACCESS_TOKEN={token_data.get('accessToken')}")
    print(f"WEBULL_REFRESH_TOKEN={token_data.get('refreshToken')}")
    print(f"WEBULL_DID={did_data}")
    print("-" * 60)
    print("\nThese tokens will bypass the need for automated logins and MFA checks on Render's cloud servers!")
else:
    print("\nPlease run the app locally and perform a successful scan once first to generate credentials.")
print("=" * 60)
