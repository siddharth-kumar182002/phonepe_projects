"""
create_table.py
Create an HBase table with pre-checks and post-checks using HappyBase.
Enhanced with ZK Master check. HDFS check removed.
"""

import argparse, sys, time
from helpers import (
    load_config, setup_logger, audit_record,
    get_happybase_connection, table_exists_happybase,
    check_zk_quorum, write_report,
    get_active_hbase_master_zk # <-- Use ZK master check
)

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--table", required=True, help="Table name to create")
    p.add_argument("--cfs", required=True, nargs="+", help="Column families (space separated)")
    p.add_argument("--config", default="config/config.yaml", help="Path to config YAML")
    return p.parse_args()

def main():
    args = parse_args()
    cfg = load_config(args.config)
    logger = setup_logger("create_table", cfg.get("logging", {}).get("log_dir", "./logs"))

    audit_record("create_table_request", cfg, {"table": args.table, "cfs": args.cfs}, logger)

    conn = None
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
        if not master_ok:
            raise RuntimeError(f"Active HBase Master check via ZK failed ({master_msg}).")
        logger.info(f"Active HBase Master found at {active_master} via ZK.")

        # 3. Table Existence Check
        logger.info(f"Checking if table '{args.table}' already exists...")
        if table_exists_happybase(cfg, args.table, logger):
             logger.error(f"Table '{args.table}' already exists. Aborting.")
             sys.exit(1) # Clean exit if table exists
        logger.info(f"Table '{args.table}' does not exist.")

        # == Create table via HappyBase ==
        logger.info(f"Attempting to create table '{args.table}' with CFs {args.cfs} via HappyBase")
        conn = get_happybase_connection(cfg, logger)
        cf_dict = {cf: dict() for cf in args.cfs}
        conn.create_table(args.table, cf_dict)
        logger.info("Create table command issued via HappyBase.")

        # == Post-checks ==
        logger.info("Running post-checks...")
        time.sleep(3) # Allow metadata propagation
        if not table_exists_happybase(cfg, args.table, logger):
            raise RuntimeError("Post-check FAILED: Table not found after create command.")
        logger.info(f"Post-check PASSED: Table '{args.table}' successfully created.")

        # == Reporting ==
        audit_record("create_table_complete", cfg, {"table": args.table, "cfs": args.cfs, "success": True}, logger)
        write_report(cfg, "create_table", {"table": args.table, "cfs": args.cfs, "result": "SUCCESS"}, logger)
        logger.info("Create table operation finished successfully.")

    except Exception as e:
        logger.exception(f"Create table operation failed: {e}")
        audit_details = {"table": args.table, "cfs": args.cfs, "error": str(e)}
        audit_record("create_table_failed", cfg, audit_details, logger)
        write_report(cfg, "create_table_failed", audit_details, logger)
        sys.exit(1)
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    main()