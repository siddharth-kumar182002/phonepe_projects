
"""
decommission_rs.py
Stops a RegionServer via Ambari, 
  - Enhanced Pre-checks (ZK, Master, Meta, RIT, Balancer, Live RS count)
  - Disable Balancer (Optional)
  - Stop RegionServer via Ambari
  - Enable Balancer (Optional)
  - Post-checks (ZK removal)
"""

import argparse, sys, time
import logging
from helpers import (
    load_config, setup_logger, audit_record,
    check_zk_quorum, write_report, # Removed get_happybase_connection, get_regions_on_server, unassign_region_happybase_fallback
    ambari_host_component_action, get_meta_server_from_zk,
    get_rs_hosts_from_zk,
    retry,
    get_active_hbase_master_zk, # ZK Cluster checks
    get_regions_in_transition_count, get_balancer_state_zk, # ZK HBase runtime checks
    set_balancer_state_salt, ambari_get_component_state # Balancer control, component state
)

# Minimum number of OTHER live RegionServers required to proceed
MIN_OTHER_LIVE_RS = 2 # Adjust based on cluster size (e.g., 1 for a 3-node cluster)

def parse_args():
    p = argparse.ArgumentParser(description="Stop an HBase RegionServer via Ambari (relies on crash recovery).")
    p.add_argument("--host", required=True, help="RegionServer hostname to stop")
    p.add_argument("--config", default="config/config.yaml")
    # Removed --wait and --check-interval as migration wait is removed
    p.add_argument("--max-rit", type=int, default=5, help="Maximum allowed RIT before aborting (default: 5). Allows checking cluster stability before stop.") # Keep RIT check
    p.add_argument("--skip-balancer", action='store_true', help="Skip disabling/enabling the balancer.")
    return p.parse_args()

# --- get_regions_on_server function is REMOVED ---

def main():
    args = parse_args()
    cfg = load_config(args.config)
    # Set logger level (DEBUG for more detail, INFO for standard)
    logger_level = logging.DEBUG # Or logging.INFO
    logger = setup_logger("stop_rs_crash_recovery", cfg.get("logging", {}).get("log_dir", "./logs"), level=logger_level) # Renamed logger slightly
    audit_record("stop_rs_request", cfg, {"host": args.host}, logger) # Renamed audit action

    start_time = time.time()
    original_balancer_state = None

    try:
        # == Pre-checks ==
        logger.info("Performing pre-stop checks...")

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

        # 3. Meta Location Check
        meta_host = get_meta_server_from_zk(cfg, logger)
        logger.info(f"hbase:meta region is hosted on: {meta_host}")
        if args.host == meta_host:
            logger.warning(f"WARNING: Target server {args.host} hosts hbase:meta. Stopping it will cause temporary cluster unavailability.")
            # Consider adding a confirmation prompt here if desired
            # confirm = input("Stop the meta server? This WILL cause downtime. (yes/no): ").lower()
            # if confirm != 'yes':
            #     logger.info("Aborting stop operation.")
            #     sys.exit(0)
        else:
            logger.info(f"Target server {args.host} does not host hbase:meta.")

        # 4. Target Server State & ZK Registration Check
        logger.info(f"Checking current state and ZK registration of {args.host}...")
        current_rs_state_ambari = ambari_get_component_state(cfg, args.host, "HBASE_REGIONSERVER", logger)
        live_rs_hosts_zk = get_rs_hosts_from_zk(cfg, logger)
        is_live_in_zk = args.host in live_rs_hosts_zk

        if current_rs_state_ambari != "STARTED" and not is_live_in_zk:
            logger.info(f"Server {args.host} is not STARTED in Ambari ({current_rs_state_ambari}) and not in ZK. Assuming already stopped. Exiting successfully.")
            elapsed_time = time.time() - start_time
            success_details = {"host": args.host, "duration_seconds": elapsed_time, "notes": "Server already stopped"}
            audit_record("stop_rs_complete", cfg, success_details, logger)
            write_report(cfg, "stop_rs", success_details, logger)
            sys.exit(0)
        elif current_rs_state_ambari != "STARTED" and is_live_in_zk:
             logger.warning(f"Server {args.host} state mismatch: Ambari={current_rs_state_ambari}, ZK=Registered. Proceeding with stop command.")
        elif current_rs_state_ambari == "STARTED" and not is_live_in_zk:
             logger.warning(f"Server {args.host} state mismatch: Ambari=STARTED, ZK=Not Registered. Proceeding with stop command.")
        elif current_rs_state_ambari is None:
             raise RuntimeError(f"HBASE_REGIONSERVER component not found on host {args.host} via Ambari. Cannot stop.")
        else: # STARTED and in ZK (or STARTED and not in ZK)
             logger.info(f"Target RegionServer {args.host} state in Ambari allows stop command.")


        # 5. Sufficient Live RegionServers Check
        other_live_rs = [h for h in live_rs_hosts_zk if h != args.host]
        logger.info(f"Found {len(other_live_rs)} other live RegionServers: {other_live_rs}")
        if len(other_live_rs) < MIN_OTHER_LIVE_RS:
             raise RuntimeError(f"Insufficient live RegionServers ({len(other_live_rs)}) remain after stopping. Need at least {MIN_OTHER_LIVE_RS} others.")

        # 6. Regions in Transition (RIT) Check (Still useful for stability check)
        logger.info("Checking for regions in transition (RIT)...")
        rit_count = get_regions_in_transition_count(cfg, logger)
        logger.info(f"Found {rit_count} regions in transition.")
        if rit_count > args.max_rit:
             raise RuntimeError(f"Found {rit_count} RIT, exceeding limit of {args.max_rit}. Aborting stop.")

        # 7. Balancer Check & Disable (Optional, less critical without migration)
        if not args.skip_balancer:
             logger.info("Checking HBase balancer state...")
             original_balancer_state = get_balancer_state_zk(cfg, logger)
             logger.info(f"HBase balancer is currently {'ENABLED' if original_balancer_state else 'DISABLED'}.")
             if original_balancer_state:
                  logger.info("Disabling HBase balancer before stopping server...")
                  set_balancer_state_salt(cfg, False, logger)
        else:
             logger.info("Skipping balancer disable/enable steps.")
             original_balancer_state = None


        # == Region Migration Section REMOVED ==
        logger.info("Skipping explicit region migration. Relying on Master crash recovery after stop.")


        # == Stop Component via Ambari ==
        logger.info(f"Proceeding to stop the HBase RegionServer component on {args.host} via Ambari...")
        ambari_host_component_action(cfg, args.host, "HBASE_REGIONSERVER", "INSTALLED")
        logger.info(f"Ambari action requested to stop HBASE_REGIONSERVER on {args.host}.")
        logger.info("Allowing some time for Ambari to process the stop command...")
        time.sleep(20) # Increased wait slightly, as stop might take longer


        # == Re-enable Balancer (if it was disabled) ==
        if original_balancer_state is True:
             logger.info("Re-enabling HBase balancer...")
             set_balancer_state_salt(cfg, True, logger)


        # == Post-checks ==
        logger.info("Performing post-stop checks (verifying ZK removal)...")
        try:
             def check_zk_removal():
                 current_rs_hosts = get_rs_hosts_from_zk(cfg, logger)
                 if args.host not in current_rs_hosts:
                     return True
                 else:
                     # Wait longer for ZK node to expire after a hard stop
                     raise ValueError(f"Host {args.host} still present in ZK /rs list. Waiting for timeout...")
             # Wait longer for ZK timeout after potential hard stop (e.g., up to 3 mins)
             retry(check_zk_removal, tries=18, delay=10, logger=logger)
             logger.info(f"Host {args.host} confirmed removed from ZooKeeper /rs list.")
        except Exception as e_zk_check:
             # This is more likely to happen if Ambari stop failed silently
             logger.warning(f"Could not confirm host removal from ZK /rs list after timeout: {e_zk_check}")
             logger.warning("Master crash recovery should still handle the regions if the server is truly down.")


        # == Final Reporting ==
        elapsed_time = time.time() - start_time
        logger.info(f"Stop flow completed for {args.host} in {elapsed_time:.2f} seconds.")
        audit_details = {"host": args.host, "duration_seconds": elapsed_time, "method": "crash_recovery"}
        audit_record("stop_rs_complete", cfg, audit_details, logger)
        write_report(cfg, "stop_rs", {"host": args.host, "status": "SUCCESS", "duration_seconds": elapsed_time, "method": "crash_recovery"}, logger)

    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.exception(f"Stop script failed after {elapsed_time:.2f} seconds: {e}")
        error_details = {"host": args.host, "duration_seconds": elapsed_time, "error": str(e)}
        audit_record("stop_rs_failed", cfg, error_details, logger)
        # Attempt to re-enable balancer if it was disabled and script failed
        if original_balancer_state is True:
            logger.warning("Attempting to re-enable balancer after script failure...")
            try: set_balancer_state_salt(cfg, True, logger)
            except Exception as e_bal: logger.error(f"Could not re-enable balancer after failure: {e_bal}")
        write_report(cfg, "stop_rs_failed", error_details, logger)
        sys.exit(1)
    finally:
        # Final check - ensure balancer is in its original state if we didn't skip
        if not args.skip_balancer and original_balancer_state is not None:
             try:
                  final_balancer_state = get_balancer_state_zk(cfg, logger)
                  if final_balancer_state != original_balancer_state:
                       logger.warning(f"Balancer final state ({final_balancer_state}) differs from original ({original_balancer_state}). Attempting restore...")
                       set_balancer_state_salt(cfg, original_balancer_state, logger)
             except Exception as e_final_bal:
                  logger.error(f"Could not verify/restore final balancer state: {e_final_bal}")

if __name__ == "__main__":
    main()
























    # """
# decommission_rs.py
# Safely decommission a RegionServer:
#   - Enhanced Pre-checks (ZK, Master, Meta, RIT, Balancer, Live RS count)
#   - Disable Balancer
#   - Unassign non-meta regions via HappyBase/Thrift fallback
#   - Wait for migration
#   - Stop RegionServer via Ambari
#   - Enable Balancer
#   - Post-checks (ZK removal)
#   - HDFS check removed.
# """

# import argparse, sys, time
# import logging # <-- ADD THIS IMPORT
# from helpers import (
#     load_config, setup_logger, audit_record,
#     check_zk_quorum, get_happybase_connection, write_report,
#     ambari_host_component_action, get_meta_server_from_zk,
#     get_rs_hosts_from_zk, unassign_region_happybase_fallback,
#     retry,
#     get_active_hbase_master_zk, # ZK Cluster checks
#     get_regions_in_transition_count, get_balancer_state_zk, # ZK HBase runtime checks
#     set_balancer_state_salt, ambari_get_component_state # Balancer control, component state
# )

# # Minimum number of OTHER live RegionServers required to proceed
# MIN_OTHER_LIVE_RS = 2 # Adjust based on cluster size (e.g., 1 for a 3-node cluster)

# def parse_args():
#     p = argparse.ArgumentParser(description="Safely decommission an HBase RegionServer.")
#     p.add_argument("--host", required=True, help="RegionServer hostname to decommission")
#     p.add_argument("--config", default="config/config.yaml")
#     p.add_argument("--wait", type=int, default=1800, help="Max wait seconds for region migration (default: 30 mins)")
#     p.add_argument("--check-interval", type=int, default=15, help="Seconds between checks during region migration (default: 15s)")
#     p.add_argument("--max-rit", type=int, default=0, help="Maximum allowed RIT before aborting (default: 0).")
#     p.add_argument("--skip-balancer", action='store_true', help="Skip disabling/enabling the balancer.")
#     return p.parse_args()

# def get_regions_on_server(cfg, host, logger):
#     """
#     Use happybase to scan hbase:meta for non-meta regions on the target server.
#     Returns a list of region names (strings).
#     """
#     server_name_str = f"{host},{cfg['hbase']['regionserver_port']}"
#     logger.debug(f"Scanning hbase:meta for non-meta regions hosted on server: {server_name_str}")
#     regions = []
#     conn = None
#     try:
#         conn = get_happybase_connection(cfg, logger)
#         meta_table = conn.table('hbase:meta')
#         scan_filter = f"ValueFilter(=, 'binaryprefix:{server_name_str}')"
#         scan_batch_size = 1000

#         for row_key, data in meta_table.scan(columns=[b'info:server'], filter=scan_filter, batch_size=scan_batch_size):
#             server_val = data.get(b'info:server')
#             if server_val and server_val.startswith(server_name_str.encode('utf-8')):
#                 try:
#                     region_name = row_key.decode('utf-8', errors='ignore')
#                     if not region_name.startswith('hbase:meta,'): # Exclude meta
#                         regions.append(region_name)
#                 except Exception as decode_err:
#                      logger.warning(f"Could not decode region name bytes: {row_key}. Error: {decode_err}")
#     except Exception as e:
#         logger.exception(f"Failed to scan hbase:meta using happybase: {e}")
#         raise
#     finally:
#         if conn:
#             conn.close()

#     unique_regions = list(set(regions))
#     logger.debug(f"Found {len(unique_regions)} unique non-meta regions on {host}.")
#     return unique_regions

# def main():
#     args = parse_args()
#     cfg = load_config(args.config)
#     # Set logger level (DEBUG for more detail, INFO for standard)
#     logger_level = logging.DEBUG # Or logging.INFO
#     logger = setup_logger("decommission_rs", cfg.get("logging", {}).get("log_dir", "./logs"), level=logger_level)
#     audit_record("decommission_request", cfg, {"host": args.host}, logger)

#     start_time = time.time()
#     original_balancer_state = None

#     try:
#         # == Pre-checks ==
#         logger.info("Performing pre-decommission checks...")

#         # 1. ZK Check
#         zk_ok, zk_details = check_zk_quorum(cfg, logger)
#         logger.info(f"ZooKeeper check: OK={zk_ok}, Details={zk_details}")
#         if not zk_ok: raise RuntimeError("ZooKeeper quorum is not healthy.")

#         # --- HDFS Check Removed ---
#         logger.info("Skipping HDFS health check.")

#         # 2. HBase Master Check (via ZK)
#         master_ok, master_msg, active_master = get_active_hbase_master_zk(cfg, logger)
#         logger.info(f"HBase Master ZK check: OK={master_ok}, Message={master_msg}")
#         if not master_ok: raise RuntimeError(f"Active HBase Master check via ZK failed ({master_msg}).")
#         logger.info(f"Active HBase Master is {active_master} via ZK.")

#         # 3. Meta Location Check
#         meta_host = get_meta_server_from_zk(cfg, logger)
#         logger.info(f"hbase:meta region is hosted on: {meta_host}")
#         if args.host == meta_host:
#             # CHANGED: Log warning instead of raising error
#             logger.warning(f"WARNING: Target server {args.host} hosts hbase:meta. Proceeding with decommission, but this is risky.")
#         else:
#             logger.info(f"Target server {args.host} does not host hbase:meta. Safe.")

#         # 4. Target Server State & ZK Registration Check
#         logger.info(f"Checking current state and ZK registration of {args.host}...")
#         current_rs_state_ambari = ambari_get_component_state(cfg, args.host, "HBASE_REGIONSERVER", logger)
#         live_rs_hosts_zk = get_rs_hosts_from_zk(cfg, logger)
#         is_live_in_zk = args.host in live_rs_hosts_zk

#         if current_rs_state_ambari != "STARTED" and not is_live_in_zk:
#             logger.info(f"Server {args.host} is not STARTED in Ambari ({current_rs_state_ambari}) and not in ZK. Assuming already decommissioned/stopped. Exiting successfully.")
#             elapsed_time = time.time() - start_time
#             success_details = {"host": args.host, "duration_seconds": elapsed_time, "notes": "Server already stopped/decommissioned"}
#             audit_record("decommission_complete", cfg, success_details, logger)
#             write_report(cfg, "decommission", success_details, logger)
#             sys.exit(0)
#         elif current_rs_state_ambari != "STARTED" and is_live_in_zk:
#              logger.warning(f"Server {args.host} state mismatch: Ambari={current_rs_state_ambari}, ZK=Registered. Proceeding cautiously.")
#         elif current_rs_state_ambari == "STARTED" and not is_live_in_zk:
#              logger.warning(f"Server {args.host} state mismatch: Ambari=STARTED, ZK=Not Registered. Proceeding cautiously, will stop via Ambari but skip region migration.")
#         else: # STARTED and in ZK
#              logger.info(f"Target RegionServer {args.host} is STARTED and registered in ZK.")


#         # 5. Sufficient Live RegionServers Check
#         other_live_rs = [h for h in live_rs_hosts_zk if h != args.host]
#         logger.info(f"Found {len(other_live_rs)} other live RegionServers: {other_live_rs}")
#         if len(other_live_rs) < MIN_OTHER_LIVE_RS:
#              raise RuntimeError(f"Insufficient live RegionServers ({len(other_live_rs)}) remain after decommissioning. Need at least {MIN_OTHER_LIVE_RS} others.")

#         # 6. Regions in Transition (RIT) Check
#         logger.info("Checking for regions in transition (RIT)...")
#         rit_count = get_regions_in_transition_count(cfg, logger)
#         logger.info(f"Found {rit_count} regions in transition.")
#         if rit_count > args.max_rit:
#              raise RuntimeError(f"Found {rit_count} RIT, exceeding limit of {args.max_rit}.")

#         # 7. Balancer Check & Disable
#         if not args.skip_balancer:
#              logger.info("Checking HBase balancer state...")
#              original_balancer_state = get_balancer_state_zk(cfg, logger)
#              logger.info(f"HBase balancer is currently {'ENABLED' if original_balancer_state else 'DISABLED'}.")
#              if original_balancer_state:
#                   logger.info("Disabling HBase balancer before region migration...")
#                   set_balancer_state_salt(cfg, False, logger)
#         else:
#              logger.info("Skipping balancer disable/enable steps.")
#              original_balancer_state = None


#         # == Region Migration ==
#         if is_live_in_zk: # Only migrate if server was registered
#              logger.info(f"Checking for regions hosted on {args.host}...")
#              regions_on_server = get_regions_on_server(cfg, args.host, logger) # Excludes meta

#              if not regions_on_server:
#                  logger.info(f"No non-meta regions found on server {args.host}.")
#              else:
#                  logger.info(f"Found {len(regions_on_server)} non-meta regions. Attempting to unassign them...")
#                  regions_failed_unassign = []
#                  for region_name in regions_on_server:
#                      try:
#                          unassign_region_happybase_fallback(cfg, region_name, logger)
#                          time.sleep(0.1)
#                      except Exception as e_unassign:
#                          logger.error(f"Failed to issue unassign for region '{region_name}': {e_unassign}")
#                          regions_failed_unassign.append(region_name)

#                  if regions_failed_unassign:
#                       logger.warning(f"Failed to issue unassign command for {len(regions_failed_unassign)} regions.")

#                  logger.info(f"Waiting up to {args.wait} seconds for non-meta region migration...")
#                  start_wait = time.time()
#                  last_remaining_count = -1
#                  while time.time() - start_wait < args.wait:
#                      time.sleep(args.check_interval)
#                      try:
#                          remaining_regions = get_regions_on_server(cfg, args.host, logger)
#                          current_remaining_count = len(remaining_regions)
#                          if current_remaining_count == 0:
#                              logger.info(f"Successfully verified all non-meta regions have moved off {args.host}.")
#                              break
#                          if current_remaining_count != last_remaining_count:
#                              logger.info(f"Migration progress: {current_remaining_count} non-meta regions remaining...")
#                              last_remaining_count = current_remaining_count
#                          else:
#                              logger.debug(f"{current_remaining_count} non-meta regions still waiting...")
#                      except Exception as e_check:
#                          logger.warning(f"Check for remaining regions failed: {e_check}. Retrying.")
#                  else: # If loop times out
#                      logger.error(f"Timeout ({args.wait}s) waiting for non-meta region migration. {last_remaining_count} regions may still be present. Proceeding with stop.")
#         else:
#              logger.info(f"Skipping region migration as {args.host} was not found in ZK /rs.")


#         # == Stop Component via Ambari ==
#         logger.info(f"Proceeding to stop the HBase RegionServer component on {args.host} via Ambari...")
#         ambari_host_component_action(cfg, args.host, "HBASE_REGIONSERVER", "INSTALLED")
#         logger.info(f"Ambari action requested to stop HBASE_REGIONSERVER on {args.host}.")
#         logger.info("Allowing some time for Ambari to process the stop command...")
#         time.sleep(10) # Simple wait


#         # == Re-enable Balancer (if it was disabled) ==
#         if original_balancer_state is True:
#              logger.info("Re-enabling HBase balancer...")
#              set_balancer_state_salt(cfg, True, logger)


#         # == Post-checks ==
#         logger.info("Performing post-decommission checks...")
#         try:
#              def check_zk_removal():
#                  current_rs_hosts = get_rs_hosts_from_zk(cfg, logger)
#                  if args.host not in current_rs_hosts:
#                      return True
#                  else:
#                      raise ValueError(f"Host {args.host} still present in ZK /rs list.")
#              retry(check_zk_removal, tries=6, delay=10, logger=logger) # Check up to 1 min
#              logger.info(f"Host {args.host} confirmed removed from ZooKeeper /rs list.")
#         except Exception as e_zk_check:
#              logger.warning(f"Could not confirm host removal from ZK /rs list after {6*10}s: {e_zk_check}")


#         # == Final Reporting ==
#         elapsed_time = time.time() - start_time
#         logger.info(f"Decommission flow completed for {args.host} in {elapsed_time:.2f} seconds.")
#         audit_details = {"host": args.host, "duration_seconds": elapsed_time}
#         audit_record("decommission_complete", cfg, audit_details, logger)
#         write_report(cfg, "decommission", {"host": args.host, "status": "SUCCESS", "duration_seconds": elapsed_time}, logger)

#     except Exception as e:
#         elapsed_time = time.time() - start_time
#         logger.exception(f"Decommission script failed after {elapsed_time:.2f} seconds: {e}")
#         error_details = {"host": args.host, "duration_seconds": elapsed_time, "error": str(e)}
#         audit_record("decommission_failed", cfg, error_details, logger)
#         # Attempt to re-enable balancer if it was disabled and script failed
#         if original_balancer_state is True:
#             logger.warning("Attempting to re-enable balancer after script failure...")
#             try: set_balancer_state_salt(cfg, True, logger)
#             except Exception as e_bal: logger.error(f"Could not re-enable balancer after failure: {e_bal}")
#         write_report(cfg, "decommission_failed", error_details, logger)
#         sys.exit(1)
#     finally:
#         # Final check - ensure balancer is in its original state if we didn't skip
#         if not args.skip_balancer and original_balancer_state is not None:
#              try:
#                   final_balancer_state = get_balancer_state_zk(cfg, logger)
#                   if final_balancer_state != original_balancer_state:
#                        logger.warning(f"Balancer final state ({final_balancer_state}) differs from original ({original_balancer_state}). Attempting restore...")
#                        set_balancer_state_salt(cfg, original_balancer_state, logger)
#              except Exception as e_final_bal:
#                   logger.error(f"Could not verify/restore final balancer state: {e_final_bal}")

# if __name__ == "__main__":
#     main()
