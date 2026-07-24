import requests
import time
import json

BASE_URL = "https://stock-screener-has6.onrender.com"

def run_test(name, start_endpoint, results_endpoint, payload):
    print(f"\n--- Testing {name} ---")
    
    # 1. Start scan
    try:
        res = requests.post(f"{BASE_URL}{start_endpoint}", json=payload)
        res_data = res.json()
        print(f"Start Response: {res.status_code} - {res_data}")
        if res.status_code != 200 or not res_data.get('ok'):
            print(f"FAILED: Could not start {name}")
            return
    except Exception as e:
        print(f"Error starting {name}: {e}")
        return

    # 2. Poll progress
    while True:
        time.sleep(2)
        try:
            prog_res = requests.get(f"{BASE_URL}/api/scan/progress")
            prog_data = prog_res.json()
            status = prog_data.get('status')
            pct = prog_data.get('pct', 0)
            phase = prog_data.get('phase_label', '')
            print(f"Progress: {pct}% - {status} ({phase})")
            
            if status == 'error':
                print(f"FAILED: Scan threw error - {phase}")
                return
            elif status == 'done':
                break
        except Exception as e:
            print(f"Error polling {name}: {e}")
            break

    # 3. Fetch results
    try:
        results_res = requests.get(f"{BASE_URL}{results_endpoint}")
        results_data = results_res.json()
        print(f"Results fetched! Status: {results_res.status_code}")
        print(f"Matches found: {results_data.get('count', 0)}")
        if results_data.get('ok'):
            print(f"SUCCESS: {name} completed perfectly.")
        else:
            print(f"FAILED: Results threw error: {results_data.get('error')}")
    except Exception as e:
        print(f"Error fetching results for {name}: {e}")

if __name__ == "__main__":
    print("Testing Watchlist Scans to ensure endpoints work without taking 3 minutes...")
    
    run_test("Regular Watchlist Scan", "/api/scan/watchlist", "/api/scan/watchlist/results", {"extended_hours": False})
    time.sleep(2)
    run_test("Options Watchlist Scan", "/api/scan/options/watchlist", "/api/scan/options/watchlist/results", {"extended_hours": False})
    time.sleep(2)
    run_test("15m Momentum Watchlist Scan", "/api/scan/momentum15m", "/api/scan/momentum15m/results", {"extended_hours": False})
    
