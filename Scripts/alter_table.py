"""
alter_table.py
Alter HBase table schema (disable -> alter -> enable) using hbase shell via Salt API.
Enhanced with ZK Master, RIT, and Balancer checks. HDFS check removed.
"""

import argparse, sys, logging
from helpers import (
    load_config, setup_logger, audit_record,
    table_exists_happybase, check_zk_quorum, write_report,
    alter_table_salt, # Uses Salt API for the operation
    get_active_hbase_master_zk, # ZK Cluster checks
    get_regions_in_transition_count, get_balancer_state_zk, # ZK HBase runtime checks
    get_happybase_connection, retry # Added for post-check
)

def parse_args():
    p = argparse.ArgumentParser(description="Alter HBase table schema using hbase shell via Salt API.")
    p.add_argument("--table", required=True, help="Table name to alter")
    p.add_argument("--alter_cmd", required=True,
                   help="The alter command arguments as used in hbase shell (e.g., \"NAME => 'cf_new'\" or \"delete => 'cf_old'\")")
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--max-rit", type=int, default=5, help="Maximum allowed regions in transition before aborting.")
    return p.parse_args()

def main():
    args = parse_args()
    cfg = load_config(args.config)
    logger = setup_logger("alter_table", cfg.get("logging", {}).get("log_dir", "./logs"), level=logging.DEBUG) # DEBUG for detail

    if not args.alter_cmd or "'" not in args.alter_cmd:
         logger.warning("Alter command string looks potentially unsafe or incomplete. Proceeding cautiously.")

    alter_details = {"table": args.table, "alter_cmd": args.alter_cmd}
    audit_record("alter_table_request", cfg, alter_details, logger)

    try:
        # == Pre-checks ==
        logger.info("Running pre-checks...")

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

        # 3. Table Existence Check
        logger.info(f"Checking if table '{args.table}' exists...")
        if not table_exists_happybase(cfg, args.table, logger):
            raise RuntimeError(f"Table '{args.table}' does not exist.")
        logger.info(f"Table '{args.table}' exists.")

        # 4. Regions in Transition (RIT) Check
        logger.info("Checking for regions in transition (RIT)...")
        rit_count = get_regions_in_transition_count(cfg, logger)
        logger.info(f"Found {rit_count} regions in transition.")
        if rit_count > args.max_rit:
             raise RuntimeError(f"Found {rit_count} RIT, exceeding limit of {args.max_rit}.")

        # 5. Balancer Check (balancer state is handled within alter_table_salt)
        logger.info("Checking HBase balancer state (will be disabled during alter if active)...")
        balancer_enabled = get_balancer_state_zk(cfg, logger)
        logger.info(f"HBase balancer is currently {'ENABLED' if balancer_enabled else 'DISABLED'}.")


        # == Alter via Salt API executing hbase shell ==
        # The alter_table_salt helper includes balancer disable/enable logic
        logger.info(f"Attempting to alter table '{args.table}' with command part: {args.alter_cmd} using Salt API...")
        alter_table_salt(cfg, args.table, args.alter_cmd, logger)

        # == Post-checks ==
        logger.info("Verifying table is enabled after alter...")
        # Add retry logic for the post-check, as enabling might take a moment
        def check_table_enabled():
            local_conn = None
            try:
                local_conn = get_happybase_connection(cfg, logger)
                if local_conn.is_table_enabled(args.table):
                    logger.info(f"Post-check PASSED: Table '{args.table}' is enabled.")
                    return True # Success
                else:
                    raise ValueError(f"Table '{args.table}' is still disabled.")
            finally:
                if local_conn: local_conn.close()

        retry(check_table_enabled, tries=6, delay=10, logger=logger) # Check for up to 1 minute

        # == Reporting ==
        logger.info("Alter command via Salt API completed successfully.")
        audit_record("alter_table_complete", cfg, alter_details, logger)
        write_report(cfg, "alter_table", {"table": args.table, "alter_cmd": args.alter_cmd, "result": "SUCCESS"}, logger)
        logger.info("Alter finished.")

    except Exception as e:
        logger.exception(f"Alter operation failed: {e}") # Log full traceback
        alter_details["error"] = str(e)
        audit_record("alter_table_failed", cfg, alter_details, logger)
        write_report(cfg, "alter_table_failed", alter_details, logger)
        sys.exit(1)
    finally:
        # Just ensure conn is closed if opened in post-check (already handled in check_table_enabled)
        pass


if __name__ == "__main__":
    main()