#!/usr/bin/env python3
"""
Webull Interactive Authentication Helper
=========================================
Authenticates with your retail Webull account (email/phone + password + MFA)
and saves fresh credentials to `webull_credentials.json` so the bot
can access your active real-time options quote subscription.

Usage:
  python3 login_webull.py
"""

import os
import sys
import getpass
import json
from dotenv import load_dotenv

# Load existing environment variables
load_dotenv()


def main():
    print("=" * 65)
    print("🔐 WEBULL AUTHENTICATION HELPER (REAL-TIME QUOTES)")
    print("=" * 65)
    print("This script logs into your Webull account to generate fresh session")
    print("tokens. Your existing real-time quote subscription will be")
    print("immediately activated for the stock screener & option trading bot.\n")

    try:
        from webull import webull
    except ImportError:
        print("❌ Error: 'webull' package not found. Run: pip install webull")
        sys.exit(1)

    app_dir = os.path.dirname(os.path.abspath(__file__))
    conf_dir = os.path.join(app_dir, "conf")
    os.makedirs(conf_dir, exist_ok=True)
    token_path = conf_dir if os.path.isdir(conf_dir) else app_dir
    credentials_file = os.path.join(token_path, "webull_credentials.json")

    # Load persistent DID first before instantiating webull so the device ID is stable
    persistent_did = None
    for p in (conf_dir, app_dir):
        df_p = os.path.join(p, "did.bin")
        if os.path.exists(df_p):
            try:
                import pickle
                with open(df_p, "rb") as df:
                    persistent_did = pickle.load(df)
                    if persistent_did:
                        break
            except Exception:
                pass
    if not persistent_did and os.getenv("WEBULL_DID"):
        persistent_did = os.getenv("WEBULL_DID").strip()

    wb = webull()
    if persistent_did:
        wb._did = persistent_did
        for p in (conf_dir, app_dir):
            try:
                import pickle
                with open(os.path.join(p, "did.bin"), "wb") as df:
                    pickle.dump(persistent_did, df)
            except Exception:
                pass

    # 1. First, check if existing token can simply be refreshed!
    token_loaded = False
    for p in (conf_dir, app_dir):
        cf = os.path.join(p, "webull_credentials.json")
        if os.path.exists(cf):
            try:
                import pickle
                with open(cf, "rb") as f:
                    token_data = pickle.load(f)
                wb._access_token = token_data.get("accessToken")
                wb._refresh_token = token_data.get("refreshToken")
                wb._uuid = token_data.get("uuid")
                token_loaded = True
                break
            except Exception:
                pass

    if not wb._refresh_token and os.getenv("WEBULL_REFRESH_TOKEN"):
        wb._refresh_token = os.getenv("WEBULL_REFRESH_TOKEN").strip()
        wb._access_token = (os.getenv("WEBULL_ACCESS_TOKEN") or "").strip()
        wb._uuid = (os.getenv("WEBULL_UUID") or "").strip()
        token_loaded = bool(wb._refresh_token)

    if wb._refresh_token and wb._refresh_token != "dummy_refresh_token_bypassed":
        try:
            print("🔄 Attempting automatic token refresh with existing refresh token...")
            res = wb.refresh_login(save_token=True, token_path=token_path)
            if res and res.get("accessToken") and wb.is_logged_in():
                print("✅ SUCCESS: Webull session successfully refreshed without re-entering password!")
                print(f"   Account ID : {wb._account_id}")
                print(f"   Saved to   : {credentials_file}\n")
                if token_path != app_dir:
                    try:
                        wb._save_token(res, app_dir)
                    except Exception:
                        pass
                _update_env_file("WEBULL_ACCESS_TOKEN", wb._access_token)
                _update_env_file("WEBULL_REFRESH_TOKEN", wb._refresh_token)
                if wb._did:
                    _update_env_file("WEBULL_DID", str(wb._did))
                _send_success_notification()
                return
            else:
                print("ℹ️ Existing refresh token expired or invalid. Full login required.\n")
        except Exception as ref_err:
            print(f"ℹ️ Auto-refresh not possible ({ref_err}). Proceeding to login.\n")

    # 2. Get username and password
    default_email = os.getenv("WEBULL_EMAIL", "")
    prompt_str = f"Enter Webull Email or Phone (e.g. +1-5551234567) [{default_email}]: " if default_email else "Enter Webull Email or Phone (e.g. +1-5551234567): "
    username = input(prompt_str).strip() or default_email
    if not username:
        print("❌ Username/email cannot be empty.")
        sys.exit(1)

    env_pwd = os.getenv("WEBULL_PASSWORD", "")
    if env_pwd:
        use_env = input("Use password configured in .env? (y/n) [default: y]: ").strip().lower()
        if use_env in ("", "y", "yes"):
            password = env_pwd
        else:
            password = getpass.getpass("Enter Webull Password: ")
    else:
        password = getpass.getpass("Enter Webull Password: ")

    if not password:
        print("❌ Password cannot be empty.")
        sys.exit(1)

    # 3. Attempt initial login
    print(f"\nLogging in as '{username}'...")
    try:
        res = wb.login(username, password, save_token=True, token_path=token_path)
    except Exception as e:
        print(f"❌ Login error: {e}")
        sys.exit(1)

    # 4. Handle MFA if required
    if "accessToken" not in res:
        print("\n📲 Multi-Factor Authentication (MFA) required.")
        print("Requesting verification code to your phone/email...")
        try:
            wb.get_mfa(username)
        except Exception as e:
            print(f"⚠️ Note requesting MFA: {e}")

        mfa_code = input("Enter the 6-digit verification code: ").strip()
        if not mfa_code:
            print("❌ Verification code cannot be empty.")
            sys.exit(1)

        print("Verifying code...")
        try:
            res = wb.login(username, password, mfa=mfa_code, save_token=True, token_path=token_path)
        except Exception as e:
            print(f"❌ Login with MFA failed: {e}")
            sys.exit(1)

    # 5. Check final result
    if "accessToken" in res and wb.is_logged_in():
        print("\n" + "=" * 65)
        print("🎉 SUCCESS: Webull account authenticated successfully!")
        print("=" * 65)
        print(f"  Account ID : {wb._account_id}")
        print(f"  Token File : {credentials_file}")
        print("\n  Updated session variables:")
        print(f"  WEBULL_ACCESS_TOKEN={wb._access_token}")
        print(f"  WEBULL_REFRESH_TOKEN={wb._refresh_token}")
        print(f"  WEBULL_DID={wb._did}")
        print("=" * 65 + "\n")

        _update_env_file("WEBULL_ACCESS_TOKEN", wb._access_token)
        _update_env_file("WEBULL_REFRESH_TOKEN", wb._refresh_token)
        if wb._did:
            _update_env_file("WEBULL_DID", str(wb._did))
        if token_path != app_dir:
            try:
                wb._save_token(res, app_dir)
            except Exception:
                pass

        _send_success_notification()
    else:
        print(f"\n❌ Login failed. Response from Webull: {res}")
        sys.exit(1)


def _update_env_file(key, value, env_path=".env"):
    """Update or append an environment variable to the local .env file."""
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r") as f:
            lines = f.readlines()
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
                prefix = "export " if stripped.startswith("export ") else ""
                new_lines.append(f"{prefix}{key}={value}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}\n")
        with open(env_path, "w") as f:
            f.writelines(new_lines)
        print(f"  [Auto-Config] Updated {key} in {env_path}")
    except Exception as e:
        print(f"  [Auto-Config] Note: Could not update {key} in {env_path}: {e}")


def _send_success_notification():
    try:
        from indicator_bot import send_telegram_notification
        send_telegram_notification(
            "✅ <b>WEBULL SESSION AUTHENTICATED</b>\n\n"
            "Fresh Webull session tokens have been verified.\n"
            "Real-time option quotes and your account subscription are now <b>ACTIVE</b>."
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
