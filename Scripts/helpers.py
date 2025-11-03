"""
helpers.py
Shared helpers for HBase BAU automation scripts.
Uses HappyBase, Kazoo (for ZK checks including Master, RIT, Balancer),
Ambari API (for control), and Salt API (for remote shell commands).
HDFS check removed.
"""

import yaml, os, sys, time, json, socket, getpass
import logging, datetime
import requests # For Ambari API and Salt API
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from kazoo.client import KazooClient, KazooState
from kazoo.exceptions import NoNodeError
import happybase # For data ops, simple admin, unassign fallback
import re

# Thrift exception import needed for the fallback function
try:
    from thrift.Thrift import TException as ThriftBaseException
except ImportError:
    ThriftBaseException = Exception
try:
    from thriftpy2.thrift import TException as ThriftPyException
except ImportError:
    ThriftPyException = ThriftBaseException

# Global variable to cache Salt token
SALT_API_TOKEN: Optional[str] = None
SALT_TOKEN_EXPIRY: float = 0

# ---------------------------
# Config & logging utilities
# ---------------------------
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
    if not logger.handlers:
        fh = logging.FileHandler(logfile)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    logger.propagate = False
    return logger

def audit_record(action: str, cfg: Dict[str, Any], details: Dict[str, Any], logger: logging.Logger) -> None:
    try:
        # CHANGED: Handle root user check
        # Check if running as root (UID 0)
        if os.geteuid() == 0:
            # If running as root, try to get the original user who ran 'sudo'
            who = os.environ.get('SUDO_USER') or os.environ.get('LOGNAME') or getpass.getuser()
            if who == 'root':
                user_display = "root (original user not found)"
            else:
                user_display = f"root (sudo by {who})"
        else:
            # Not root, just get the current user
            user_display = getpass.getuser()
    except Exception:
        # Fallback in case os.geteuid() fails (e.g., non-Unix)
        user_display = getpass.getuser()

    rec = {
        "timestamp_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "user": user_display,
        "action": action,
        "details": details
    }
    logger.info("AUDIT: %s", json.dumps(rec))
    audit_dir = cfg.get("logging", {}).get("log_dir", "./logs")
    try:
        os.makedirs(audit_dir, exist_ok=True)
        with open(os.path.join(audit_dir, "audit.log"), "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        logger.error(f"Failed to write audit record to file: {e}")


# ---------------------------
# Ambari helpers (Control Only)
# ---------------------------
def ambari_base(cfg: Dict[str,Any]) -> str:
    host = cfg['ambari']['host']
    port = cfg['ambari'].get('port', 8080)
    protocol = cfg['ambari'].get('protocol', 'http')
    return f"{protocol}://{host}:{port}/api/v1/"

def ambari_get(cfg: Dict[str,Any], path: str, timeout=15) -> Dict:
    # This might still be needed for ambari_get_component_state
    base = ambari_base(cfg)
    url = base + path.lstrip("/")
    verify = cfg['ambari'].get('verify_ssl', True)
    logger = logging.getLogger(__name__)
    logger.debug(f"Ambari GET: {url}")
    try:
        r = requests.get(url, auth=(cfg['ambari']['username'], cfg['ambari']['password']), timeout=timeout, verify=verify)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Ambari GET request failed: {e}")
        raise

def ambari_put(cfg: Dict[str,Any], path: str, payload: Dict, timeout=30) -> Dict:
    base = ambari_base(cfg)
    url = base + path.lstrip("/")
    verify = cfg['ambari'].get('verify_ssl', True)
    logger = logging.getLogger(__name__)
    logger.debug(f"Ambari PUT: {url} | Payload: {json.dumps(payload)}")
    try:
        r = requests.put(url, auth=(cfg['ambari']['username'], cfg['ambari']['password']),
                         json=payload, timeout=timeout, headers={"X-Requested-By":"hbase-bau"}, verify=verify)
        r.raise_for_status()
        try:
            return r.json()
        except json.JSONDecodeError:
             logger.debug("Ambari PUT response was not JSON, returning raw text.")
             return {"raw_response": r.text}
    except requests.exceptions.RequestException as e:
        logger.error(f"Ambari PUT request failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
             logger.error(f"Ambari PUT Response Body: {e.response.text}")
        raise

def ambari_host_component_action(cfg: Dict[str,Any], host: str, component: str, desired_state: str) -> Dict:
    """Sets Ambari host component state (e.g., INSTALLED, STARTED)."""
    logger = logging.getLogger(__name__)
    logger.info(f"Requesting Ambari to set {component} on {host} to {desired_state}")
    path = f"clusters/{cfg['ambari']['cluster_name']}/hosts/{host}/host_components/{component}"
    payload = {
        "RequestInfo": {"context": f"Set {component} on {host} to {desired_state} via script"},
        "Body": {"HostRoles": {"state": desired_state}}
    }
    return ambari_put(cfg, path, payload)

def ambari_get_component_state(cfg: Dict[str,Any], host: str, component: str, logger: logging.Logger) -> Optional[str]:
    """Gets the state (e.g., STARTED, INSTALLED) of a specific component on a host via Ambari."""
    logger.debug(f"Getting Ambari state for {component} on {host}")
    cluster = cfg['ambari']['cluster_name']
    path = f"clusters/{cluster}/hosts/{host}/host_components/{component}?fields=HostRoles/state"
    try:
        data = ambari_get(cfg, path)
        state = data.get("HostRoles", {}).get("state")
        logger.debug(f"Ambari state for {component} on {host} is '{state}'")
        return state
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.warning(f"Component {component} not found on host {host} via Ambari.")
            return None
        else:
            logger.error(f"Error getting Ambari component state for {component} on {host}: {e}")
            raise
    except Exception as e:
        logger.error(f"Error getting Ambari component state for {component} on {host}: {e}")
        raise

# ---------------------------
# ZooKeeper helpers (using Kazoo)
# ---------------------------
def get_zk_hosts_str(cfg: Dict[str,Any]) -> str:
    zk_hosts = cfg['hbase']['zk_quorum']
    zk_port = int(cfg['hbase'].get('zk_port', 2181))
    return ",".join([f"{h}:{zk_port}" for h in zk_hosts])

def get_zk_client(cfg: Dict[str,Any], logger: logging.Logger, timeout: int = 15) -> KazooClient:
    zk_conn_str = get_zk_hosts_str(cfg)
    try:
        logging.getLogger("kazoo").setLevel(logging.WARNING)
        retry_policy = {'max_tries': 3, 'delay': 0.5, 'backoff': 2, 'max_delay': 5}
        zk = KazooClient(hosts=zk_conn_str, timeout=10.0, logger=logger, connection_retry=retry_policy, command_retry=retry_policy)
        logger.debug(f"Connecting Kazoo client to {zk_conn_str}...")
        zk.start(timeout=timeout)
        if not zk.connected:
            raise ConnectionError(f"Kazoo client did not connect to {zk_conn_str} within {timeout}s.")
        logger.debug(f"Kazoo connected state: {zk.state}")
        return zk
    except Exception as e:
        logger.error(f"Failed to connect Kazoo to ZooKeeper at {zk_conn_str}: {e}")
        raise

def check_zk_quorum(cfg: Dict[str,Any], logger: logging.Logger, timeout: int = 30) -> Tuple[bool, Dict[str,Any]]:
    """Checks ZK health by attempting to connect and get basic info."""
    details = {}
    zk = None
    try:
        zk = get_zk_client(cfg, logger, timeout=timeout)
        details['state'] = zk.state
        try:
             details['server_version'] = zk.server_version()
        except Exception as e_ver:
             logger.warning(f"Could not get ZK server version: {e_ver}")
             details['server_version'] = "Unknown"
        is_ok = details['state'] == KazooState.CONNECTED
        return is_ok, details
    except Exception as e:
        logger.error(f"ZooKeeper check failed: {e}", exc_info=False)
        details['error'] = str(e)
        return False, details
    finally:
        if zk:
            try:
                zk.stop()
                zk.close()
            except Exception as e_close:
                logger.warning(f"Error closing ZK connection: {e_close}")

def get_meta_server_from_zk(cfg: Dict[str,Any], logger: logging.Logger) -> str:
    """Finds the active hbase:meta server hostname from ZooKeeper using regex."""
    zk_parent = cfg['hbase']['zk_hbase_parent_znode']
    zk_path = f"{zk_parent}/meta-region-server"
    zk = None
    try:
        zk = get_zk_client(cfg, logger)
        if not zk.exists(zk_path):
            logger.warning(f"Meta znode not found at configured path {zk_path}, trying /hbase/meta-region-server")
            zk_path = "/hbase/meta-region-server"
            if not zk.exists(zk_path):
                raise FileNotFoundError(f"Could not find meta znode at {cfg['hbase']['zk_hbase_parent_znode']}/meta-region-server or /hbase/meta-region-server")

        data_bytes, stat = zk.get(zk_path)
        logger.debug(f"Raw data from ZK node {zk_path}: {data_bytes}")
        # Look for the specific hostname pattern (adjust regex if needed)
        match = re.search(rb'stg-hdpsiddharth\d+\.phonepe\.nb6', data_bytes)
        if match:
            server_host = match.group(0).decode('utf-8')
            logger.info(f"Found meta-region-server at {server_host} from ZK (regex match).")
            return server_host
        else:
            logger.error(f"Could not find hostname pattern in ZK data: {data_bytes}")
            raise ValueError(f"Could not parse server name from meta znode data using regex.")
    except Exception as e:
        logger.exception(f"Failed to get or parse hbase:meta location from ZK: {e}")
        raise
    finally:
        if zk:
            try:
                zk.stop()
                zk.close()
            except Exception as e_close:
                 logger.warning(f"Error closing ZK connection: {e_close}")

def get_rs_hosts_from_zk(cfg: Dict[str,Any], logger: logging.Logger) -> List[str]:
    """Gets the list of live RegionServers hostnames from ZooKeeper."""
    zk_parent = cfg['hbase']['zk_hbase_parent_znode']
    zk_path = f"{zk_parent}/rs"
    zk = None
    try:
        zk = get_zk_client(cfg, logger)
        if not zk.exists(zk_path):
             logger.warning(f"RegionServer znode not found at configured path {zk_path}, trying /hbase/rs")
             zk_path = "/hbase/rs"
             if not zk.exists(zk_path):
                 raise FileNotFoundError(f"Could not find /rs znode at {cfg['hbase']['zk_hbase_parent_znode']}/rs or /hbase/rs")
        servers = zk.get_children(zk_path)
        hosts = [s.split(',')[0] for s in servers if ',' in s]
        logger.debug(f"Found {len(hosts)} region servers in ZK under {zk_path}: {hosts}")
        return hosts
    except Exception as e:
        logger.exception(f"Failed to get /rs list from ZK: {e}")
        return []
    finally:
        if zk:
            zk.stop()
            zk.close()

def get_active_hbase_master_zk(cfg: Dict[str, Any], logger: logging.Logger) -> Tuple[bool, str, Optional[str]]:
    """Checks for an active HBase Master via ZooKeeper. Returns (is_ok, status_message, active_master_host)."""
    logger.info("Checking active HBase Master via ZooKeeper...")
    zk_parent = cfg['hbase']['zk_hbase_parent_znode']
    # Common paths for active master: 'master' (HBase 1.x, 2.x) or 'splitWAL' (sometimes contains master info too)
    # Let's prioritize the 'master' znode
    master_path = f"{zk_parent}/master"
    zk = None
    active_master_host = None
    try:
        zk = get_zk_client(cfg, logger)
        if zk.exists(master_path):
            data_bytes, stat = zk.get(master_path)
            logger.debug(f"Raw data from ZK node {master_path}: {data_bytes}")

            # Data format is PBUF. Use regex to extract hostname.
            match = re.search(rb'stg-hdpsiddharth\d+\.phonepe\.nb6', data_bytes)
            if match:
                active_master_host = match.group(0).decode('utf-8')
                status_message = f"Active HBase Master found at {active_master_host} via ZK node {master_path}."
                logger.info(status_message)
                return True, status_message, active_master_host
            else:
                logger.error(f"Could not parse hostname from ZK master node data: {data_bytes}")
                status_message = f"Active Master znode {master_path} exists but hostname could not be parsed."
                return False, status_message, None
        else:
            status_message = f"Active Master znode '{master_path}' does not exist. No active Master found."
            logger.error(status_message)
            return False, status_message, None
    except NoNodeError:
        status_message = f"Active Master znode '{master_path}' does not exist. No active Master found."
        logger.error(status_message)
        return False, status_message, None
    except Exception as e:
        logger.error(f"Failed to check active HBase Master via ZK: {e}", exc_info=True)
        return False, f"Failed to check active Master via ZK: {e}", None
    finally:
        if zk:
            zk.stop()
            zk.close()

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
            zk.stop()
            zk.close()

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
            zk.stop()
            zk.close()

# --- HBase HappyBase helpers ---
def get_happybase_connection(cfg: Dict[str,Any], logger: logging.Logger) -> happybase.Connection:
    host = cfg['hbase']['thrift']['host']; port = int(cfg['hbase']['thrift']['port'])
    try:
        with socket.create_connection((host, port), timeout=5): pass
        logger.debug(f"Thrift server probe successful: {host}:{port}")
    except Exception as e:
        logger.warning(f"Thrift server probe failed: {host}:{port} -> {e}")

    try:
        conn = happybase.Connection(host=host, port=port, timeout=30000, autoconnect=False)
        conn.open()
        logger.debug(f"HappyBase connection opened successfully to {host}:{port}.")
        return conn
    except Exception as e:
        logger.error(f"HappyBase connection failed to {host}:{port}: {e}", exc_info=True)
        raise

def table_exists_happybase(cfg: Dict[str,Any], table_name: str, logger: logging.Logger) -> bool:
    conn = None
    try:
        conn = get_happybase_connection(cfg, logger)
        tables = conn.tables()
        decoded_tables = [t.decode('utf-8') if isinstance(t, bytes) else t for t in tables]
        exists = table_name in decoded_tables
        logger.debug(f"Table '{table_name}' exists check via HappyBase: {exists}. (Tables found: {decoded_tables})")
        return exists
    except Exception as e:
        logger.error(f"Could not check table existence for '{table_name}' via HappyBase: {e}", exc_info=True)
        raise
    finally:
        if conn:
            conn.close()

def unassign_region_happybase_fallback(cfg: Dict[str, Any], region_name: str, logger: logging.Logger):
    """
    Fallback to unassign a region using 'hbase shell' via Salt API.
    (Replaces direct Thrift attempt which was unreliable).
    """
    logger.info(f"Attempting to unassign region {region_name} via Salt/hbase shell (force=true)...")
    target_minion = cfg['salt']['hbase_shell_target']
    # Escape single quotes in region name for shell safety
    safe_region_name = region_name.replace("'", "'\\''")
    hbase_cmd = f"unassign '{safe_region_name}', true" # Force = true
    salt_cmd = f'echo "{hbase_cmd}" | hbase shell -n' # Use double quotes for echo

    try:
        run_salt_command(cfg, target_minion, 'cmd.run', [salt_cmd], logger, timeout=60)
        logger.info(f"Successfully issued unassign command via Salt for region {region_name}")
    except Exception as e:
        logger.exception(f"Salt command execution failed during unassign for region {region_name}: {e}")
        # Re-raise the exception so the calling script knows it failed
        raise RuntimeError(f"Failed to issue unassign via Salt for {region_name}: {e}")
   
# --- Salt API Helpers ---
def _get_salt_token(cfg: Dict[str, Any], logger: logging.Logger) -> str:
    """Logs into Salt API and returns the token, caching it."""
    global SALT_API_TOKEN, SALT_TOKEN_EXPIRY
    if SALT_API_TOKEN and time.time() < (SALT_TOKEN_EXPIRY - 300):
        logger.debug("Using cached Salt API token.")
        return SALT_API_TOKEN

    salt_url = cfg['salt']['api_url']
    username = cfg['salt']['api_user']
    password = cfg['salt']['api_password']
    login_url = f"{salt_url}/login"
    payload = {"username": username, "password": password, "eauth": "pam"}
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}

    logger.info(f"Requesting new Salt API token from {salt_url} for user '{username}'...")
    try:
        # verify=False for self-signed certs
        response = requests.post(login_url, json=payload, headers=headers, timeout=15, verify=False)
        response.raise_for_status()
        data = response.json()
        token_info = data.get('return', [])[0]
        token = token_info.get('token')
        expiry = token_info.get('expire')
        if not token or not expiry: raise ValueError("Salt API login response missing token or expiry.")
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
        if 'data' in locals(): logger.error(f"Response data: {data}")
        raise ValueError("Could not extract token/expiry from Salt API response.")

def run_salt_command(cfg: Dict[str, Any], target: str, function: str, args: List[Any], logger: logging.Logger, timeout: int = 300) -> Dict[str, Any]:
    """Runs a Salt execution module function via the Salt API. Improved error checking."""
    token = _get_salt_token(cfg, logger)
    salt_url = cfg['salt']['api_url']
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Auth-Token': token}
    payload = [{"client": "local", "tgt": target, "fun": function, "arg": args}]

    logger.info(f"Executing Salt command: tgt='{target}', fun='{function}', args='{args}'")
    try:
        http_timeout = timeout + 30
        response = requests.post(salt_url, json=payload, headers=headers, timeout=http_timeout, verify=False) # verify=False for self-signed
        response.raise_for_status()
        result_data = response.json()

        if 'return' not in result_data or not result_data['return']:
            raise ValueError("Salt API response missing 'return' data or is empty.")
        results = result_data['return'][0]
        logger.debug(f"Raw Salt command result: {results}")

        if not results: raise RuntimeError(f"Salt command failed: No response from target '{target}'.")
        if target not in results: raise RuntimeError(f"Salt command failed: Target '{target}' did not return result. Full response: {results}")

        minion_result = results[target]

        if minion_result is False: raise RuntimeError(f"Salt command '{function}' failed explicitly on minion '{target}' (returned False).")
        if minion_result is None: logger.warning(f"Salt command '{function}' returned None for minion '{target}'.")

        if function == 'cmd.run':
            if isinstance(minion_result, str):
                logger.info(f"Salt cmd.run output from '{target}': {minion_result[:500]}...")
                output_upper = minion_result.upper()
                # Check for errors more reliably using keywords found in hbase shell failures
                error_keywords = ["ERROR:", "EXCEPTION", "ILLEGALARGUMENT", "NAMENOTFOUND", "TABLEDISABLED", "TABLENOTENABLED"]
                if any(keyword in output_upper for keyword in error_keywords):
                    logger.error(f"Salt command '{function}' output on '{target}' indicates failure:\n{minion_result}")
                    raise RuntimeError(f"Salt command failed on minion '{target}'. Output indicates error.")
                # Check for non-empty stderr (often warnings in shell)
                stderr_match = re.search(r"STDERR:(.*)", minion_result, re.IGNORECASE | re.DOTALL)
                if stderr_match and stderr_match.group(1).strip():
                     logger.warning(f"Salt command '{function}' on '{target}' produced stderr output:\n{stderr_match.group(1).strip()}")
            else:
                raise TypeError(f"Salt cmd.run on '{target}' returned unexpected type '{type(minion_result)}'. Value: {minion_result}")

        return results

    except requests.exceptions.RequestException as e:
        logger.error(f"Salt API command execution failed (HTTP Error): {e}")
        if hasattr(e, 'response') and e.response is not None: logger.error(f"Salt API Response Status: {e.response.status_code}, Body: {e.response.text}")
        raise ConnectionError(f"Could not execute command via Salt API at {salt_url}")
    except Exception as e:
        logger.exception(f"Unexpected error during Salt command execution or result processing: {e}")
        raise

def set_balancer_state_salt(cfg: Dict[str, Any], enabled: bool, logger: logging.Logger):
    """Enables or disables the HBase balancer using hbase shell via Salt."""
    state_str = "true" if enabled else "false"
    action_str = "Enabling" if enabled else "Disabling"
    target_minion = cfg['salt']['hbase_shell_target']
    hbase_cmd = f"balance_switch {state_str}"
    # Use single quotes around hbase_cmd for shell safety
    salt_cmd = f"echo '{hbase_cmd}' | hbase shell -n"

    logger.info(f"{action_str} HBase balancer via Salt on {target_minion}...")
    try:
        run_salt_command(cfg, target_minion, 'cmd.run', [salt_cmd], logger, timeout=60)
        logger.info(f"HBase balancer state change to {enabled} requested successfully.")
        time.sleep(3) # Give ZK a moment to update
        # Verify state change via ZK
        current_state = get_balancer_state_zk(cfg, logger)
        if current_state != enabled:
             logger.warning(f"Balancer state in ZK ({current_state}) does not match desired state ({enabled}) shortly after command execution.")
        else:
             logger.info(f"Balancer state confirmed as {enabled} in ZK.")

    except Exception as e:
        logger.error(f"Failed to set balancer state to {enabled} via Salt: {e}")
        logger.warning("Proceeding despite potential balancer state change failure.")


# --- HBase Alter Helper using Salt API ---
def alter_table_salt(cfg: Dict[str, Any], table_name: str, alter_cmd_str: str, logger: logging.Logger):
    """
    Alters an HBase table using 'hbase shell' commands executed via Salt API.
    Handles balancer disable/enable and table disable -> alter -> enable cycle.
    """
    target_minion = cfg['salt']['hbase_shell_target']
    
    # CHANGED: Removed complex escaping. Build the literal hbase shell strings.
    disable_script = f"disable '{table_name}'"
    alter_script = f"alter '{table_name}', {alter_cmd_str}" # Use the raw command string
    enable_script = f"enable '{table_name}'"

    # CHANGED: Use double quotes for the 'echo' command to preserve inner single quotes.
    disable_cmd_salt = f'echo "{disable_script}" | hbase shell -n'
    alter_cmd_salt = f'echo "{alter_script}" | hbase shell -n'
    enable_cmd_salt = f'echo "{enable_script}" | hbase shell -n'

    original_balancer_state = None
    try:
        # Check and disable balancer
        original_balancer_state = get_balancer_state_zk(cfg, logger)
        if original_balancer_state:
            logger.info("Balancer is enabled, disabling before alter.")
            set_balancer_state_salt(cfg, False, logger)

        # 1. Disable Table
        logger.info(f"Disabling table {table_name} via Salt on {target_minion}...")
        run_salt_command(cfg, target_minion, 'cmd.run', [disable_cmd_salt], logger, timeout=120)
        logger.info(f"Disable command sent successfully.")

        # 2. Alter Table
        logger.info(f"Altering table {table_name} with '{alter_cmd_str}' via Salt...")
        run_salt_command(cfg, target_minion, 'cmd.run', [alter_cmd_salt], logger, timeout=180)
        logger.info(f"Alter command sent successfully.")

    except Exception as e:
        logger.exception(f"Disable or Alter command failed via Salt: {e}")
        logger.warning(f"Attempting to re-enable table {table_name} after alter failure...")
        try:
            run_salt_command(cfg, target_minion, 'cmd.run', [enable_cmd_salt], logger, timeout=120)
            logger.info(f"Table {table_name} re-enabled after failed alter attempt.")
        except Exception as e_enable:
            logger.critical(f"CRITICAL: Failed to re-enable table {table_name} via Salt after alter failed. MANUAL INTERVENTION REQUIRED. Error: {e_enable}")
        if original_balancer_state is True:
             logger.warning("Attempting to re-enable balancer after failed alter...")
             set_balancer_state_salt(cfg, True, logger)
        raise

    # 3. Enable Table (if disable/alter succeeded)
    logger.info(f"Enabling table {table_name} via Salt...")
    try:
        run_salt_command(cfg, target_minion, 'cmd.run', [enable_cmd_salt], logger, timeout=120)
        logger.info(f"Enable command sent successfully.")
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to enable table {table_name} via Salt after successful alter. MANUAL INTERVENTION REQUIRED. Error: {e}")
        if original_balancer_state is True:
             logger.warning("Attempting to re-enable balancer after failed table enable...")
             set_balancer_state_salt(cfg, True, logger)
        raise
    finally:
         # Always restore balancer state if it was originally enabled
         if original_balancer_state is True:
              logger.info("Re-enabling balancer after alter operation completion.")
              set_balancer_state_salt(cfg, True, logger)
# --- Misc helpers ---
def retry(func, tries=3, delay=3, backoff=2, logger=None):
    """Generic retry helper with exponential backoff."""
    _tries = tries
    _delay = delay
    while _tries > 0:
        try:
            return func()
        except Exception as e:
            _tries -= 1
            msg = f"Operation failed: {e}. Retrying in {_delay:.2f}s... ({_tries} tries left)"
            if logger: logger.warning(msg)
            else: print(f"WARNING: {msg}", file=sys.stderr)
            if _tries <= 0:
                if logger: logger.error("Retries exhausted.")
                raise
            time.sleep(_delay)
            _delay *= backoff

def write_report(cfg: Dict[str,Any], name: str, payload: Dict[str,Any], logger: logging.Logger):
    """Writes a JSON report to the log directory."""
    log_dir = cfg.get('logging', {}).get('log_dir', './logs')
    path = None
    try:
        os.makedirs(log_dir, exist_ok=True)
        safe_name = name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        timestamp = int(time.time())
        path = os.path.join(log_dir, f"report_{safe_name}_{timestamp}.json")
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        logger.info(f"Report written to {path}")
        return path
    except Exception as e:
        logger.error(f"Failed to write report '{name}' to path '{path}': {e}", exc_info=True)
        return None