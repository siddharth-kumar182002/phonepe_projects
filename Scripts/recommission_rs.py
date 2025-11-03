"""
recommission_rs.py
Recommission a stopped RegionServer:
  - Enhanced Pre-checks (ZK, Master, Target State)
  - Start component via Ambari
  - Verify ZK Registration
  - HDFS check removed.
"""

import argparse, sys, time
import logging 
from helpers import (
    load_config, setup_logger, audit_record,
    check_zk_quorum, ambari_host_component_action,
    write_report, get_rs_hosts_from_zk, retry,
    get_active_hbase_master_zk, # ZK Cluster checks
    ambari_get_component_state # Component state check
)

def parse_args():
    p = argparse.ArgumentParser(description="Recommission a stopped HBase RegionServer.")
    p.add_argument("--host", required=True, help="RegionServer hostname to recommission")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--wait", type=int, default=300, help="Max wait seconds for server to appear in ZK (default: 5 mins)")
    p.add_argument("--check-interval", type=int, default=10, help="Seconds between ZK checks (default: 10s)")
    return p.parse_args()

def main():
    args = parse_args()
    cfg = load_config(args.config)
    logger = setup_logger("recommission_rs", cfg.get("logging", {}).get("log_dir", "./logs"), level=logging.DEBUG) # Use DEBUG
    audit_record("recommission_request", cfg, {"host": args.host}, logger)

    start_time = time.time()
    try:
        # == Pre-checks ==
        logger.info("Performing pre-recommission checks...")

        # 1. ZK Check
        zk_ok, zk_details = check_zk_quorum(cfg, logger)
        logger.info(f"ZooKeeper check: OK={zk_ok}, Details={zk_details}")
        if not zk_ok: raise RuntimeError("ZooKeeper quorum is not healthy.")

        # --- HDFS Check Removed ---
        logger.info("Skipping HDFS health check.")

        # 2. HBase Master Check (via ZK)
        master_ok, master_msg, active_master = get_active_hbase_master_zk(cfg, logger)
        logger.info(f"HBase Master ZK check: OK={master_ok}, Message={master_msg}")
        if not master_ok: raise RuntimeError(f"Active HBase Master check via ZK failed ({master_msg}).")
        logger.info(f"Active HBase Master is {active_master} via ZK.")

        # 3. Target Server State Check (ensure it's stopped/installed or handle if already started)
        logger.info(f"Checking current Ambari state of HBASE_REGIONSERVER on {args.host}...")
        current_rs_state = ambari_get_component_state(cfg, args.host, "HBASE_REGIONSERVER", logger)

        if current_rs_state == "STARTED":
             logger.warning(f"Target RegionServer {args.host} is already STARTED according to Ambari.")
             # Check if it's actually registered in ZK
             current_rs_hosts_zk = get_rs_hosts_from_zk(cfg, logger)
             if args.host in current_rs_hosts_zk:
                  logger.info(f"Server {args.host} is STARTED and registered in ZK. Recommission already complete. Exiting successfully.")
                  elapsed_time = time.time() - start_time
                  success_details = {"host": args.host, "duration_seconds": elapsed_time, "notes": "Server already started/recommissioned"}
                  audit_record("recommission_complete", cfg, success_details, logger)
                  write_report(cfg, "recommission", success_details, logger)
                  sys.exit(0)
             else:
                  logger.warning(f"Server {args.host} is STARTED in Ambari but not registered in ZK. Will attempt START command anyway to possibly trigger registration.")
        elif current_rs_state is None:
             raise RuntimeError(f"HBASE_REGIONSERVER component not found on host {args.host} via Ambari. Cannot recommission.")
        elif current_rs_state != "INSTALLED":
             logger.warning(f"Target RegionServer {args.host} is in an unexpected state '{current_rs_state}' (expected INSTALLED). Attempting start anyway.")
        else: # INSTALLED state
            logger.info(f"Target RegionServer {args.host} is currently INSTALLED (stopped). Proceeding with start.")


        # == Start Component via Ambari ==
        logger.info(f"Instructing Ambari to START HBASE_REGIONSERVER on {args.host}...")
        ambari_host_component_action(cfg, args.host, "HBASE_REGIONSERVER", "STARTED")
        logger.info("Ambari start command requested successfully.")
        logger.info("Allowing time for Ambari/Agent and RegionServer startup...")
        time.sleep(15) # Increased initial wait


        # == Wait for ZK Registration ==
        logger.info(f"Waiting up to {args.wait}s for {args.host} to appear in ZooKeeper /rs znode...")
        def check_zk_registration():
            current_rs_hosts = get_rs_hosts_from_zk(cfg, logger)
            if args.host in current_rs_hosts:
                logger.info(f"Server {args.host} successfully registered in ZooKeeper.")
                return True
            else:
                 raise ValueError(f"Host {args.host} not yet found in ZK /rs list: {current_rs_hosts}")

        num_tries = max(1, (args.wait - 15) // args.check_interval) # Adjust tries based on initial wait
        retry(check_zk_registration, tries=num_tries, delay=args.check_interval, logger=logger)
        # If retry fails, it raises an exception caught below


        # == Final Success Reporting ==
        elapsed_time = time.time() - start_time
        logger.info(f"Recommission completed successfully for {args.host} in {elapsed_time:.2f} seconds.")
        success_details = {"host": args.host, "duration_seconds": elapsed_time}
        audit_record("recommission_complete", cfg, success_details, logger)
        write_report(cfg, "recommission", {"host": args.host, "status": "SUCCESS", "duration_seconds": elapsed_time}, logger)

    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.exception(f"Recommission script failed after {elapsed_time:.2f} seconds: {e}")
        error_details = {"host": args.host, "duration_seconds": elapsed_time, "error": str(e)}
        audit_record("recommission_failed", cfg, error_details, logger)
        write_report(cfg, "recommission_failed", error_details, logger)
        sys.exit(1)


if __name__ == "__main__":
    main()