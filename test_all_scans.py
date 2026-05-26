"""
Test all scan endpoints on the local Flask server.
Starts each scan, polls progress, and prints results.
"""
import requests
import time
import json
import sys

BASE = "http://localhost:5050"

def test_endpoint(name, start_url, results_url, method="POST"):
    """Start a scan, poll progress, then fetch results."""
    print(f"\n{'='*60}")
    print(f"  TEST: {name}")
    print(f"{'='*60}")

    # Start the scan
    print(f"  → Starting scan: POST {start_url}")
    resp = requests.post(f"{BASE}{start_url}", json={"extended_hours": False}, timeout=10)
    print(f"  ← Status: {resp.status_code}")
    data = resp.json()
    print(f"  ← Response: {json.dumps(data, indent=2)}")

    if not data.get("ok"):
        print(f"  ✗ FAILED to start: {data.get('error')}")
        return False

    # Poll progress
    print(f"\n  Polling progress...")
    max_wait = 300  # 5 minutes max
    start_time = time.time()
    last_phase = ""

    while time.time() - start_time < max_wait:
        time.sleep(3)
        try:
            prog = requests.get(f"{BASE}/api/scan/progress", timeout=5).json()
            phase = prog.get("phase_label", "")
            status = prog.get("status", "")
            pct = prog.get("pct", 0)
            found = prog.get("found", 0)

            if phase != last_phase:
                print(f"  [{pct:3.0f}%] {phase}  (found: {found})")
                last_phase = phase

            if status in ("done", "error"):
                break
        except Exception as e:
            print(f"  Poll error: {e}")
            break

    elapsed = time.time() - start_time
    print(f"  Scan completed in {elapsed:.1f}s")

    # Fetch results
    print(f"\n  → Fetching results: GET {results_url}")
    resp = requests.get(f"{BASE}{results_url}", timeout=10)
    print(f"  ← Status: {resp.status_code}")
    data = resp.json()

    if data.get("ok"):
        count = data.get("count", 0)
        results = data.get("results", [])
        print(f"  ✓ SUCCESS — {count} result(s) found")
        if results:
            # Print first 3 results summary
            for i, r in enumerate(results[:3]):
                ticker = r.get("Ticker", "?")
                grade = r.get("Grade", r.get("Score", "N/A"))
                price = r.get("Last Price", "?")
                print(f"    #{i+1}: {ticker} @ ${price}  Grade/Score: {grade}")
            if count > 3:
                print(f"    ... and {count - 3} more")
        else:
            print(f"  (no setups matched filters)")
        return True
    else:
        print(f"  ✗ FAILED: {data.get('error')}")
        return False


def main():
    # 1. Test API connectivity
    print("Testing API connectivity...")
    try:
        resp = requests.get(f"{BASE}/api/test", timeout=10)
        print(f"  API test: {resp.status_code} - {resp.json().get('status', 'unknown')}")
    except Exception as e:
        print(f"  Cannot reach server: {e}")
        print(f"  Make sure the app is running on port 5050")
        sys.exit(1)

    # 2. Check watchlist
    resp = requests.get(f"{BASE}/api/watchlist", timeout=5)
    wl = resp.json().get("watchlist", [])
    print(f"  Watchlist: {len(wl)} tickers — {wl[:5]}...")

    results = {}

    # 3. Test each scan type
    scans = [
        ("Reversal Watchlist Scan", "/api/scan", "/api/scan/results"),
        ("Options Watchlist Scan", "/api/scan/options", "/api/scan/options/results"),
    ]

    for name, start_url, results_url in scans:
        ok = test_endpoint(name, start_url, results_url)
        results[name] = "✓ PASS" if ok else "✗ FAIL"
        time.sleep(2)  # Brief pause between scans

    # Summary
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    for name, status in results.items():
        print(f"  {status}  {name}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
