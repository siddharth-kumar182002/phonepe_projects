import sys
import logging
import yaml
import os
import time
import json
import socket
import getpass
import datetime
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

# --- Copied Imports from helpers.py ---
import requests
from kazoo.client import KazooClient, KazooState
from kazoo.exceptions import NoNodeError
import happybase
try:
    from thrift.Thrift import TException as ThriftBaseException
except ImportError:
    ThriftBaseException = Exception
try:
    from thriftpy2.thrift import TException as ThriftPyException
except ImportError:
    ThriftPyException = ThriftBaseException
# --- End Copied Imports ---


# --- Configuration ---
CONFIG_PATH = "config/config.yaml" # Make sure this points to your config file
# --- End Configuration ---

# --- Global variable copied from helpers.py ---
SALT_API_TOKEN: Optional[str] = None
SALT_TOKEN_EXPIRY: float = 0
# --- End Global variable ---

# --------------------------------------------------
# --- COPIED FUNCTIONS FROM helpers.py START ---
# --------------------------------------------------

# --- Config & logging utilities (already present or slightly modified) ---
def load_config(path: str = "config/config.yaml") -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p.resolve()}")
    with open(p) as f:
        return yaml.safe_load(f)

def setup_logger(name: str, log_dir: str, level=logging.INFO) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    logfile = os.path.join(log_dir, f"{name}_{timestamp}.log")
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # Avoid adding handlers multiple times if logger already exists
    if not logger.handlers:
        fh = logging.FileHandler(logfile)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        # Add console handler only if not already present from main script setup
        if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            logger.addHandler(sh)
    logger.propagate = False # Prevent double logging if root logger is configured
    return logger

# --- ZooKeeper helpers ---
def get_zk_hosts_str(cfg: Dict[str,Any]) -> str:
    zk_hosts = cfg['hbase']['zk_quorum']
    zk_port = int(cfg['hbase'].get('zk_port', 2181))
    return ",".join([f"{h}:{zk_port}" for h in zk_hosts])

def get_zk_client(cfg: Dict[str,Any], logger: logging.Logger, timeout: int = 15) -> KazooClient:
    zk_conn_str = get_zk_hosts_str(cfg)
    try:
        # Reduce kazoo verbosity
        logging.getLogger("kazoo").setLevel(logging.WARNING)
        # Define retry policies for connection and commands
        retry_policy = {'max_tries': 3, 'delay': 0.5, 'backoff': 2, 'max_delay': 5}
        zk = KazooClient(hosts=zk_conn_str, timeout=10.0, logger=logger, connection_retry=retry_policy, command_retry=retry_policy)
        logger.debug(f"Connecting Kazoo client to {zk_conn_str}...")
        zk.start(timeout=timeout) # Attempt connection with timeout
        if not zk.connected:
            raise ConnectionError(f"Kazoo client did not connect to {zk_conn_str} within {timeout}s.")
        logger.debug(f"Kazoo connected state: {zk.state}")
        return zk
    except Exception as e:
        logger.error(f"Failed to connect Kazoo to ZooKeeper at {zk_conn_str}: {e}")
        raise # Re-raise the exception

def get_regions_in_transition_count(cfg: Dict[str, Any], logger: logging.Logger) -> int:
    """Checks the number of regions currently in transition via ZooKeeper."""
    zk_parent = cfg['hbase']['zk_hbase_parent_znode']
    # Common paths: 'region-in-transition' or 'rit'
    rit_paths_to_check = [f"{zk_parent}/region-in-transition", f"{zk_parent}/rit"]
    zk = None
    logger.debug(f"Checking for regions in transition (RIT) in ZK...")
    try:
        zk = get_zk_client(cfg, logger)
        for rit_path in rit_paths_to_check:
            if zk.exists(rit_path):
                rit_regions = zk.get_children(rit_path)
                count = len(rit_regions)
                logger.debug(f"Found {count} RIT under '{rit_path}': {rit_regions if count <= 10 else rit_regions[:10] + ['...']}")
                return count
            else:
                logger.debug(f"RIT znode '{rit_path}' does not exist.")

        # If none of the paths exist
        logger.debug("No RIT znodes found, assuming 0 regions in transition.")
        return 0
    except NoNodeError: # Should be caught by exists check, but just in case
        logger.debug("RIT znode does not exist during get_children, assuming 0.")
        return 0
    except Exception as e:
        logger.error(f"Failed to check regions in transition: {e}", exc_info=True)
        raise RuntimeError(f"Could not determine regions in transition count: {e}")
    finally:
        if zk:
            try:
                zk.stop()
                zk.close()
            except Exception as e_close:
                 logger.warning(f"Error closing ZK connection for RIT check: {e_close}")

def get_balancer_state_zk(cfg: Dict[str, Any], logger: logging.Logger) -> bool:
    """Checks if the HBase load balancer is enabled via ZooKeeper (presence of znode)."""
    zk_parent = cfg['hbase']['zk_hbase_parent_znode']
    balancer_path = f"{zk_parent}/balancer"
    zk = None
    logger.debug(f"Checking balancer state via existence of ZK path: {balancer_path}")
    try:
        zk = get_zk_client(cfg, logger)
        exists = zk.exists(balancer_path)
        if exists:
            logger.debug(f"Balancer znode '{balancer_path}' exists, assuming balancer is ENABLED.")
            # Optionally log the raw data for debugging if needed in the future
            # try:
            #     data, stat = zk.get(balancer_path)
            #     logger.debug(f"Raw balancer znode data: {data!r}") # Use !r for raw bytes
            # except Exception as e_get:
            #     logger.warning(f"Could not get data from existing balancer znode: {e_get}")
            return True
        else:
            logger.debug(f"Balancer znode '{balancer_path}' does not exist, assuming balancer is DISABLED.")
            return False
    except Exception as e:
        logger.error(f"Failed to check balancer state via znode existence: {e}", exc_info=True)
        # In case of error, maybe safer to assume disabled or raise error
        # Let's raise, as the state is unknown
        raise RuntimeError(f"Could not determine balancer state due to ZK error: {e}")
    finally:
        if zk:
            try:
                zk.stop()
                zk.close()
            except Exception as e_close:
                 logger.warning(f"Error closing ZK connection for balancer check: {e_close}")

# --- HBase HappyBase helpers ---
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

# --- Salt API Helpers ---
def _get_salt_token(cfg: Dict[str, Any], logger: logging.Logger) -> str:
    """Logs into Salt API and returns the token, caching it."""
    global SALT_API_TOKEN, SALT_TOKEN_EXPIRY
    # Check if cached token exists and is not expiring soon (within 5 mins)
    if SALT_API_TOKEN and time.time() < (SALT_TOKEN_EXPIRY - 300):
        logger.debug("Using cached Salt API token.")
        return SALT_API_TOKEN

    salt_url = cfg['salt']['api_url']
    username = cfg['salt']['api_user']
    password = cfg['salt']['api_password']
    login_url = f"{salt_url}/login"
    payload = {"username": username, "password": password, "eauth": "pam"} # Assuming PAM auth
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}

    logger.info(f"Requesting new Salt API token from {salt_url} for user '{username}'...")
    try:
        # Consider making SSL verification configurable
        response = requests.post(login_url, json=payload, headers=headers, timeout=15, verify=False) # verify=False for self-signed certs often
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        data = response.json()
        # Parse the standard Salt API login response structure
        token_info = data.get('return', [])[0]
        token = token_info.get('token')
        expiry = token_info.get('expire') # Expiry is usually a Unix timestamp
        if not token or not expiry: raise ValueError("Salt API login response missing token or expiry.")
        # Cache the token and expiry time
        SALT_API_TOKEN = token
        SALT_TOKEN_EXPIRY = expiry
        logger.info("Successfully obtained Salt API token.")
        return token
    except requests.exceptions.RequestException as e:
        logger.error(f"Salt API login failed: {e}")
        if hasattr(e, 'response') and e.response is not None: logger.error(f"Salt API Response: {e.response.text}")
        raise ConnectionError(f"Could not log in to Salt API at {salt_url}")
    except (IndexError, KeyError, ValueError) as e:
        logger.error(f"Failed to parse Salt API login response: {e}")
        if 'data' in locals(): logger.error(f"Response data: {data}") # Log response if parsing failed
        raise ValueError("Could not extract token/expiry from Salt API response.")

def run_salt_command(cfg: Dict[str, Any], target: str, function: str, args: List[Any], logger: logging.Logger, timeout: int = 300) -> Dict[str, Any]:
    """Runs a Salt execution module function via the Salt API. Improved error checking."""
    token = _get_salt_token(cfg, logger)
    salt_url = cfg['salt']['api_url']
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Auth-Token': token}
    # Standard Salt API payload structure for running commands
    payload = [{"client": "local", "tgt": target, "fun": function, "arg": args}]

    logger.info(f"Executing Salt command: tgt='{target}', fun='{function}', args='{args}'")
    try:
        # Set a slightly longer HTTP timeout than the expected command duration
        http_timeout = timeout + 30
        response = requests.post(salt_url, json=payload, headers=headers, timeout=http_timeout, verify=False) # verify=False for self-signed
        response.raise_for_status()
        result_data = response.json()

        # Basic validation of the response structure
        if 'return' not in result_data or not result_data['return']:
            raise ValueError("Salt API response missing 'return' data or is empty.")
        # 'return' is a list containing results per target group/type
        results = result_data['return'][0]
        logger.debug(f"Raw Salt command result: {results}")

        # Check if the specific target we expected actually returned a result
        if not results: # Empty dictionary if no minions matched or responded
            raise RuntimeError(f"Salt command failed: No response received from any targets matching '{target}'.")
        if target not in results:
            # Maybe the target spec was wrong, or the minion didn't respond in time
            raise RuntimeError(f"Salt command failed: Target '{target}' did not return a result. Full response: {results}")

        minion_result = results[target]

        # Explicit failure indicated by Salt (e.g., function not available)
        if minion_result is False:
             raise RuntimeError(f"Salt command '{function}' failed explicitly on minion '{target}' (returned False).")
        # None might indicate success for some modules, but often worth noting
        if minion_result is None:
             logger.warning(f"Salt command '{function}' returned None for minion '{target}'. Assuming success but inspect if unexpected.")

        # Specific checks for 'cmd.run' output
        if function == 'cmd.run':
            if isinstance(minion_result, str):
                logger.info(f"Salt cmd.run output from '{target}': {minion_result[:500]}...") # Log truncated output
                output_upper = minion_result.upper()
                # Check for common error indicators in shell output
                error_keywords = ["ERROR:", "EXCEPTION", "ILLEGALARGUMENT", "NAMENOTFOUND", "TABLEDISABLED", "TABLENOTENABLED", "FAILED", "FAILURE"]
                if any(keyword in output_upper for keyword in error_keywords):
                    # Log the full output if an error keyword is found
                    logger.error(f"Salt command '{function}' output on '{target}' indicates failure:\n{minion_result}")
                    raise RuntimeError(f"Salt command failed on minion '{target}'. Output indicates error.")
                # Check for anything printed to stderr
                # This regex might need adjustment depending on how Salt captures stderr
                stderr_match = re.search(r"STDERR:(.*)", minion_result, re.IGNORECASE | re.DOTALL)
                if stderr_match and stderr_match.group(1).strip():
                     logger.warning(f"Salt command '{function}' on '{target}' produced stderr output (may or may not be an error):\n{stderr_match.group(1).strip()}")
            else:
                # cmd.run should return a string, raise error if not
                raise TypeError(f"Salt cmd.run on '{target}' returned unexpected type '{type(minion_result)}'. Value: {minion_result}")

        # If we passed all checks, return the result dictionary
        return results

    except requests.exceptions.RequestException as e:
        logger.error(f"Salt API command execution failed (HTTP Error): {e}")
        if hasattr(e, 'response') and e.response is not None: logger.error(f"Salt API Response Status: {e.response.status_code}, Body: {e.response.text}")
        raise ConnectionError(f"Could not execute command via Salt API at {salt_url}")
    except Exception as e:
        # Catch any other unexpected errors during processing
        logger.exception(f"Unexpected error during Salt command execution or result processing: {e}")
        raise

# --- HBase Unassign Helper (using Salt fallback) ---
def unassign_region_happybase_fallback(cfg: Dict[str, Any], region_name: str, logger: logging.Logger):
    """
    Fallback to unassign a region using 'hbase shell' via Salt API.
    (Replaces direct Thrift attempt which was unreliable).
    """
    logger.info(f"Attempting to unassign region {region_name} via Salt/hbase shell (force=true)...")
    target_minion = cfg['salt']['hbase_shell_target']
    # Escape single quotes in region name for shell safety, e.g., ' becomes '\''
    safe_region_name = region_name.replace("'", "'\\''")
    hbase_cmd = f"unassign '{safe_region_name}', true" # Force = true
    # Use double quotes around the echo'd command to preserve inner single quotes
    salt_cmd = f'echo "{hbase_cmd}" | hbase shell -n'

    try:
        # Execute the command via Salt
        run_salt_command(cfg, target_minion, 'cmd.run', [salt_cmd], logger, timeout=60)
        logger.info(f"Successfully issued unassign command via Salt for region {region_name}")
    except Exception as e:
        # Log the specific error and re-raise a more generic one
        logger.exception(f"Salt command execution failed during unassign for region {region_name}: {e}")
        raise RuntimeError(f"Failed to issue unassign via Salt for {region_name}: {e}")
    # No HappyBase connection to close here anymore

# ------------------------------------------------
# --- COPIED FUNCTIONS FROM helpers.py END ---
# ------------------------------------------------


# --- Test Script Specific Functions ---
def find_a_region_to_unassign(cfg, logger):
    """Finds a non-meta, non-namespace region name to test unassign."""
    conn = None
    try:
        conn = get_happybase_connection(cfg, logger)
        meta_table = conn.table('hbase:meta')
        logger.info("Scanning hbase:meta to find a sample region...")
        # Scan for *any* non-meta, non-namespace region
        for row_key, data in meta_table.scan(limit=100): # Limit scan for speed
            try:
                region_name = row_key.decode('utf-8', errors='ignore')
                # Exclude meta and namespace table regions as they are critical
                if not region_name.startswith('hbase:meta,') and not region_name.startswith('hbase:namespace,'):
                     # Check if it has server info (is likely online)
                    if data.get(b'info:server'):
                        logger.info(f"Found sample region to unassign: {region_name}")
                        return region_name
            except Exception:
                continue # Ignore decoding errors etc.
        logger.error("Could not find a suitable non-meta, non-namespace region to test unassign within the first 100 meta rows.")
        return None
    except Exception as e:
        logger.exception(f"Error scanning hbase:meta: {e}")
        return None
    finally:
        if conn:
            conn.close()

# --- Main Test Execution ---
if __name__ == "__main__":
    try:
        cfg = load_config(CONFIG_PATH)
    except FileNotFoundError:
        print(f"ERROR: Configuration file not found at {CONFIG_PATH}. Please create or adjust the path.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to load configuration: {e}")
        sys.exit(1)

    # Use INFO level for less verbose output during normal testing
    logger = setup_logger("test_helpers", cfg.get("logging", {}).get("log_dir", "./logs"), level=logging.INFO)

    print("\n--- Testing get_balancer_state_zk ---")
    try:
        balancer_state = get_balancer_state_zk(cfg, logger)
        print(f"✅ Balancer state reported by function: {balancer_state}")
        print("   -> Verify this matches the actual state (use 'balance_switch true|false' in hbase shell to change and re-run).")
    except Exception as e:
        logger.exception("get_balancer_state_zk failed")
        print(f"❌ ERROR: get_balancer_state_zk failed: {e}")

    print("\n--- Testing get_regions_in_transition_count ---")
    try:
        rit_count = get_regions_in_transition_count(cfg, logger)
        print(f"✅ Regions in transition reported by function: {rit_count}")
        print("   -> Should be 0 when idle. To test non-zero, run 'move \"REGION_NAME\"' in hbase shell IMMEDIATELY before running this script.")
    except Exception as e:
        logger.exception("get_regions_in_transition_count failed")
        print(f"❌ ERROR: get_regions_in_transition_count failed: {e}")

    print("\n--- Testing unassign_region_happybase_fallback ---")
    # Find a region to test with first
    region_to_test = find_a_region_to_unassign(cfg, logger)

    if region_to_test:
        print(f"ℹ️ Attempting to unassign sample region: {region_to_test}")
        print("   ‼️ This WILL move the region off its current server ‼️")
        try:
            confirm = input("   Proceed? (yes/no): ").lower().strip()
            if confirm == 'yes':
                try:
                    # Call the now local unassign function
                    unassign_region_happybase_fallback(cfg, region_to_test, logger)
                    print(f"✅ Unassign command issued via Salt for {region_to_test}.")
                    print("   -> Verify in HBase Master UI: the region should briefly be in transition or assigned to a different server.")
                except Exception as e:
                    # Error during the actual unassign call
                    logger.exception("unassign_region_happybase_fallback failed")
                    print(f"❌ ERROR: unassign_region_happybase_fallback failed: {e}")
            else:
                print("⏭️ Skipping unassign test.")
        except EOFError: # Handle case where input is piped or unavailable
             print("⏭️ Skipping unassign test (no input received for confirmation).")
    else:
        print("⚠️ Could not find a suitable region to test unassign. Skipping.")

    print("\n--- Tests Complete ---")