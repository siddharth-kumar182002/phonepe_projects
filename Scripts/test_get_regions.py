import sys
import logging
import argparse
import yaml # Need yaml for load_config
import os # Need os for path joining
import socket # Need socket for happybase helper
import time # Need time for happybase helper (though not used directly here)
import happybase # Need happybase itself
from pathlib import Path
from typing import Dict, Any, List, Optional

# --- Configuration ---
CONFIG_PATH = "config/config.yaml" # Make sure this points to your config file
# --- End Configuration ---


def load_config(path: str = "config/config.yaml") -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p.resolve()}")
    with open(p) as f:
        return yaml.safe_load(f)

def setup_logger(name: str, log_dir: str, level=logging.INFO) -> logging.Logger:
    # Simplified logger setup for testing
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger(name)
    # Prevent adding handlers multiple times if logger already exists
    # If using basicConfig, we usually don't need file handlers unless specified
    logger.propagate = False
    return logger

def get_happybase_connection(cfg: Dict[str,Any], logger: logging.Logger) -> happybase.Connection:
    host = cfg['hbase']['thrift']['host']; port = int(cfg['hbase']['thrift']['port'])
    # Optional: Probe Thrift server port first
    try:
        with socket.create_connection((host, port), timeout=5): pass
        logger.debug(f"Thrift server probe successful: {host}:{port}")
    except Exception as e:
        logger.warning(f"Thrift server probe failed: {host}:{port} -> {e}")

    # Attempt HappyBase connection
    try:
        # Use a reasonable timeout (e.g., 30 seconds = 30000 ms)
        # autoconnect=False initially, manually open to catch errors better
        conn = happybase.Connection(host=host, port=port, timeout=30000, autoconnect=False)
        conn.open() # Explicitly open the connection
        logger.debug(f"HappyBase connection opened successfully to {host}:{port}.")
        return conn
    except Exception as e:
        logger.error(f"HappyBase connection failed to {host}:{port}: {e}", exc_info=True)
        raise # Re-raise the exception

# --- THE FUNCTION TO TEST ---
# (Keep imports and other copied functions as they were)

def get_regions_on_server(cfg: Dict[str, Any], host: str, logger: logging.Logger) -> List[str]:
    """
    Use happybase to scan hbase:meta for non-meta regions on the target server.
    Filters client-side for robustness. Returns a list of region names (strings).
    Includes detailed logging AND PRINT STATEMENTS of scanned values.
    """
    server_name_str = f"{host}:{cfg['hbase']['regionserver_port']}"
    server_name_bytes = server_name_str.encode('utf-8')
    logger.info(f"Scanning hbase:meta client-side for regions hosted on server: {server_name_str}")
    print(f"DEBUG PRINT: Will compare 'info:server' start against byte string: {server_name_bytes!r}") # FORCE PRINT

    regions_found = []
    conn = None
    try:
        conn = get_happybase_connection(cfg, logger)
        meta_table = conn.table('hbase:meta')
        scan_batch_size = 500

        logger.info("Starting scan of 'hbase:meta', columns=['info:server']...")
        count_scanned = 0
        count_matched_prefix = 0

        for row_key, data in meta_table.scan(columns=[b'info:server'], batch_size=scan_batch_size):
            count_scanned += 1
            server_val_bytes = data.get(b'info:server')

            # --- FORCE PRINT STATEMENTS ---
            region_name_str_log = row_key.decode('utf-8', 'ignore')
            server_val_str_log = server_val_bytes.decode('utf-8', 'ignore') if server_val_bytes else "None"
            # Print directly to console, bypassing logger
            print(f"DEBUG PRINT: Row {count_scanned}: Region='{region_name_str_log}', Raw Server Bytes={server_val_bytes!r}")
            # --- END FORCE PRINT ---

            # Perform the check in Python
            if server_val_bytes and server_val_bytes.startswith(server_name_bytes):
                count_matched_prefix += 1
                print(f"DEBUG PRINT:   -> MATCH FOUND for server prefix {server_name_bytes!r}") # FORCE PRINT
                try:
                    region_name = row_key.decode('utf-8', errors='ignore')
                    if not region_name.startswith('hbase:meta,'):
                        regions_found.append(region_name)
                        print(f"DEBUG PRINT:     -> Added region: {region_name}") # FORCE PRINT
                    # else: print(f"DEBUG PRINT:     -> Skipping meta region: {region_name}")
                except Exception as decode_err:
                     logger.warning(f"Could not decode region name bytes {row_key!r} for matched server. Error: {decode_err}")

        logger.info(f"Scan complete. Scanned {count_scanned} total rows, found {count_matched_prefix} rows matching server prefix '{server_name_str}'.")

    except Exception as e:
        logger.exception(f"Failed to scan hbase:meta using happybase: {e}")
        raise
    finally:
        if conn:
            conn.close()
            logger.debug("HappyBase connection closed.")

    unique_regions = list(set(regions_found))
    logger.info(f"Found {len(unique_regions)} unique non-meta regions on {host}.")
    return unique_regions

# (Keep the rest of test_get_regions.py as it was)

# ----------------------------------------------
# --- COPIED FUNCTIONS FROM helpers.py END ---
# ----------------------------------------------

# --- Main Test Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the get_regions_on_server function.")
    parser.add_argument("--host", required=True, help="RegionServer hostname to check")
    args = parser.parse_args()

    try:
        cfg = load_config(CONFIG_PATH)
    except FileNotFoundError:
        print(f"ERROR: Configuration file not found at {CONFIG_PATH}.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to load configuration: {e}")
        sys.exit(1)

    # Use DEBUG level to see detailed logs from the function
    logger = setup_logger("test_get_regions", cfg.get("logging", {}).get("log_dir", "./logs"), level=logging.DEBUG)

    print(f"\n--- Testing get_regions_on_server for host: {args.host} ---")
    try:
        start_time = time.time()
        regions_found = get_regions_on_server(cfg, args.host, logger)
        end_time = time.time()

        print(f"\n✅ Function completed in {end_time - start_time:.2f} seconds.")
        print(f"Found {len(regions_found)} non-meta regions:")
        if regions_found:
            for region in sorted(regions_found): # Sort for easier comparison
                print(f"  - {region}")
        else:
            print("  (None)")

        print("\n   -> Verification Step:")
        print(f"   1. Open the HBase Master UI.")
        print(f"   2. Click on the 'Region Servers' tab.")
        print(f"   3. Find the server: {args.host}")
        print(f"   4. Compare the number and names of non-meta regions listed there with the output above.")
        print(f"   (Note: UI might include system tables like hbase:namespace sometimes).")

    except Exception as e:
        logger.exception("get_regions_on_server failed during test")
        print(f"\n❌ ERROR: get_regions_on_server failed: {e}")

    print("\n--- Test Complete ---")