#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Import common code
from sysadmws_common import *
import gitlab
import glob
import textwrap
import subprocess
import filelock
import pytz
from datetime import datetime

# Constants and envs

LOGO="Jobs"
WORK_DIR = os.environ.get("ACC_WORKDIR", "/opt/sysadmws/accounting")
LOG_DIR = os.environ.get("ACC_LOGDIR", "/opt/sysadmws/accounting/log")
LOG_FILE = "jobs.log"
TARIFFS_SUBDIR = "tariffs"
CLIENTS_SUBDIR = "clients"
YAML_GLOB = "*.yaml"
YAML_EXT = "yaml"
ACC_YAML = "accounting.yaml"
HISTORY_JSON = ".jobs/history.json"
HISTORY_LOCK = ".jobs/history.lock"
LOCK_TIMEOUT = 600 # Supposed to be run each 10 minutes, so lock for 10 minutes
MINUTES_JITTER = 10 # Jobs are run on some minute between 00 and 10 minutes each 10 minutes

# Main

if __name__ == "__main__":

    # Set parser and parse args
    parser = argparse.ArgumentParser(description='{LOGO} functions.'.format(LOGO=LOGO))
    parser.add_argument("--debug", dest="debug", help="enable debug", action="store_true")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-jobs", dest="run_jobs", help="run jobs for server SERVER (use ALL for all servers) via GitLab pipelines for CLIENT (use ALL for all clients)", nargs=2, metavar=("CLIENT", "SERVER"))
    group.add_argument("--force-run-job", dest="force_run_job", help="force run (omit time conditions) specific job id JOB for server SERVER (use ALL for all servers) via GitLab pipelines for CLIENT (use ALL for all clients)", nargs=3, metavar=("CLIENT", "SERVER", "JOB"))
    group.add_argument("--prune-run-tags", dest="prune_run_tags", help="prune all run_* tags older than AGE via GitLab API for CLIENT (use ALL for all clients)", nargs=2, metavar=("CLIENT", "AGE"))
    args = parser.parse_args()

    # Set logger and console debug
    if args.debug:
        logger = set_logger(logging.DEBUG, LOG_DIR, LOG_FILE)
    else:
        logger = set_logger(logging.ERROR, LOG_DIR, LOG_FILE)

    GL_ADMIN_PRIVATE_TOKEN = os.environ.get("GL_ADMIN_PRIVATE_TOKEN")
    if GL_ADMIN_PRIVATE_TOKEN is None:
        raise Exception("Env var GL_ADMIN_PRIVATE_TOKEN missing")
    
    errors = False

    # Catch exception to logger

    try:

        logger.info("Starting {LOGO}".format(LOGO=LOGO))

        # Chdir to work dir
        os.chdir(WORK_DIR)

        # Read ACC_YAML
        acc_yaml_dict = load_yaml("{0}/{1}".format(WORK_DIR, ACC_YAML), logger)
        if acc_yaml_dict is None:
            raise Exception("Config file error or missing: {0}/{1}".format(WORK_DIR, ACC_YAML))
        
        # Do tasks

        if args.run_jobs or args.force_run_job:

            # Save now once in UTC
            # We cannot take now() within run jobs loops - each job run takes ~5 secs and thats why now drifts many minutes forward
            saved_now = datetime.now(pytz.timezone("UTC"))
            
            # Connect to GitLab
            gl = gitlab.Gitlab(acc_yaml_dict["gitlab"]["url"], private_token=GL_ADMIN_PRIVATE_TOKEN)
            gl.auth()

            # For *.yaml in client dir
            for client_file in glob.glob("{0}/{1}".format(CLIENTS_SUBDIR, YAML_GLOB)):

                # Client file errors should not stop other clients
                try:
                
                    logger.info("Found client file: {0}".format(client_file))

                    # Load client YAML
                    client_dict = load_yaml("{0}/{1}".format(WORK_DIR, client_file), logger)
                    if client_dict is None:
                        raise Exception("Config file error or missing: {0}/{1}".format(WORK_DIR, client_file))
                    
                    # Skip other clients
                    if args.run_jobs:
                        run_client, run_server = args.run_jobs
                    if args.force_run_job:
                        run_client, run_server, run_job = args.force_run_job

                    if run_client != "ALL" and client_dict["name"].lower() != run_client:
                        continue

                    # Skip disabled clients
                    if not client_dict["active"]:
                        continue

                    # Skip clients without salt_project
                    if "salt_project" not in client_dict["gitlab"]:
                        logger.info("Salt project not defined for client {client}, skipping".format(client=client_dict["name"]))
                        continue
                    
                    # Skip clients with jobs disabled
                    if "jobs_disabled" in client_dict and client_dict["jobs_disabled"]:
                        logger.info("Jos disabled for client {client}, skipping".format(client=client_dict["name"]))
                        continue

                    # Get GitLab project for client
                    project = gl.projects.get(client_dict["gitlab"]["salt_project"]["path"])
                    logger.info("Salt project {project} for client {client} ssh_url_to_repo: {ssh_url_to_repo}, path_with_namespace: {path_with_namespace}".format(project=client_dict["gitlab"]["salt_project"]["path"], client=client_dict["name"], path_with_namespace=project.path_with_namespace, ssh_url_to_repo=project.ssh_url_to_repo))

                    # Make server list
                    servers_list = []
                    if "servers" in client_dict:
                        servers_list.extend(client_dict["servers"])
                    if client_dict["configuration_management"]["type"] == "salt":
                        servers_list.extend(client_dict["configuration_management"]["salt"]["masters"])

                    # For each server
                    for server in servers_list:

                        # Server errors should not stop other servers
                        try:

                            # Skip servers if needed
                            if run_server != "ALL" and server["fqdn"] != run_server:
                                continue
                            
                            # Skip servers with jobs disabled
                            if "jobs_disabled" in server and server["jobs_disabled"]:
                                logger.info("Jos disabled for server {server}, skipping".format(server=server["fqdn"]))
                                continue
                            
                            # Skip not active servers
                            if "active" in server and not server["active"]:
                                logger.info("Server {server} is not active, skipping".format(server=server["fqdn"]))
                                continue
                            
                            # Build job list
                            job_list = []

                            # Add global jobs from accounting yaml
                            if "jobs" in acc_yaml_dict:
                                
                                for job_id, job_params in acc_yaml_dict["jobs"].items():
                                    
                                    # Do not add if the same job exists in client jobs or server jobs
                                    if not (("jobs" in client_dict and job_id in client_dict["jobs"]) or ("jobs" in server and job_id in server["jobs"])):
                                        job_params["id"] = job_id
                                        job_params["level"] = "GLOBAL"
                                        job_list.append(job_params)

                            # Add client jobs from client yaml
                            if "jobs" in client_dict:
                                
                                for job_id, job_params in client_dict["jobs"].items():
                                    
                                    # Do not add if the same job exists in server jobs
                                    if not ("jobs" in server and job_id in server["jobs"]):
                                        job_params["id"] = job_id
                                        job_params["level"] = "CLIENT"
                                        job_list.append(job_params)

                            # Add server jobs from server def in client yaml
                            if "jobs" in server:
                                
                                for job_id, job_params in server["jobs"].items():
                                    job_params["id"] = job_id
                                    job_params["level"] = "SERVER"
                                    job_list.append(job_params)

                            # Run jobs from job list

                            logger.info("Job list for server {server}:".format(server=server["fqdn"]))
                            logger.info(json.dumps(job_list, indent=4, sort_keys=True))

                            for job in job_list:

                                # Check os include
                                if "os" in job and "include" in job["os"]:
                                    if server["os"] not in job["os"]["include"]:
                                        logger.info("Job {server}/{job} skipped because os {os} is not in job os include list".format(server=server["fqdn"], job=job["id"], os=server["os"]))
                                        continue

                                # Check os exclude
                                if "os" in job and "exclude" in job["os"]:
                                    if server["os"] in job["os"]["exclude"]:
                                        logger.info("Job {server}/{job} skipped because os {os} is in job os exclude list".format(server=server["fqdn"], job=job["id"], os=server["os"]))
                                        continue

                                # Check job is disabled
                                if "disabled" in job and job["disabled"]:
                                    logger.info("Job {server}/{job} skipped because it is disabled".format(server=server["fqdn"], job=job["id"]))
                                    continue

                                # Check licenses
                                if "licenses" in job:
                                    
                                    logger.info("Job {server}/{job} requires license list {lic_list_job}, loading licenses in tariffs".format(server=server["fqdn"], job=job["id"], lic_list_job=job["licenses"]))

                                    # Load tariffs

                                    # Take the first (upper and current) tariff
                                    all_tar_lic_list = []
                                    for server_tariff in server["tariffs"][0]["tariffs"]:

                                        # If tariff has file key - load it
                                        if "file" in server_tariff:
                                            
                                            tariff_dict = load_yaml("{0}/{1}/{2}".format(WORK_DIR, TARIFFS_SUBDIR, server_tariff["file"]), logger)
                                            if tariff_dict is None:
                                                
                                                raise Exception("Tariff file error or missing: {0}/{1}".format(WORK_DIR, server_tariff["file"]))

                                            # Add tariff plan licenses to all tariffs lic list if exist
                                            if "licenses" in tariff_dict:
                                                all_tar_lic_list.extend(tariff_dict["licenses"])

                                        # Also take inline plan and service
                                        else:

                                            # Add tariff plan licenses to all tariffs lic list if exist
                                            if "licenses" in server_tariff:
                                                all_tar_lic_list.extend(server_tariff["licenses"])

                                    # Search for all needed licenses in tariff licenses and skip if not found
                                    if not all(lic in all_tar_lic_list for lic in job["licenses"]):
                                        logger.info("Job {server}/{job} skipped because required license list {lic_list_job} is not found in joined licenses {lic_list_tar} of all of server tariffs".format(server=server["fqdn"], job=job["id"], lic_list_job=job["licenses"], lic_list_tar=all_tar_lic_list))
                                        continue
                                    else:
                                        logger.info("Job {server}/{job} required license list {lic_list_job} is found in joined licenses {lic_list_tar} of all of server tariffs".format(server=server["fqdn"], job=job["id"], lic_list_job=job["licenses"], lic_list_tar=all_tar_lic_list))

                                # Lock before trying to open, exception and exit on timeout is ok
                                with filelock.FileLock(HISTORY_LOCK).acquire(timeout=LOCK_TIMEOUT):
                                    
                                    # Job error should not stop other jobs
                                    try:

                                        # Make now from saved_now in job timezone
                                        now = saved_now.astimezone(pytz.timezone(job["tz"]))
                                        logger.info("Job {server}/{job} now() in job TZ is {now}".format(server=server["fqdn"], job=job["id"], now=datetime.strftime(now, "%Y-%m-%d %H:%M:%S %z %Z")))

                                        # Try to load last job run from history file if file exists
                                        try:
                                            with open(HISTORY_JSON, "r") as history_json:
                                                history_dict = json.load(history_json)
                                        # Else just init server in dict on any error
                                        except:
                                            history_dict = {}

                                        # Get job last run
                                        if server["fqdn"] in history_dict and job["id"] in history_dict[server["fqdn"]]:
                                            # There is some bug with strptime on python 3.6
                                            # While on 3.5 strptime %z %Z works, pn 3.6 - not, so we remove last word before converting str to date and do not use last %Z - it is actually only for human reading
                                            job_last_run = datetime.strptime(history_dict[server["fqdn"]][job["id"]].rsplit(' ', 1)[0], "%Y-%m-%d %H:%M:%S %z")
                                        else:
                                            job_last_run =  datetime.strptime("1970-01-01 00:00:00 +0000", "%Y-%m-%d %H:%M:%S %z")
                                        logger.info("Job {server}/{job} last run: {time}".format(server=server["fqdn"], job=job["id"], time=datetime.strftime(job_last_run, "%Y-%m-%d %H:%M:%S %z %Z")))
                                        
                                        # Check force run

                                        if args.force_run_job:

                                            if job["id"] != run_job:
                                                logger.info("Job {server}/{job} skipped because job id didn't match force run parameter".format(server=server["fqdn"], job=job["id"]))
                                                continue
                                            logger.info("Job {server}/{job} force run - time conditions omitted".format(server=server["fqdn"], job=job["id"]))

                                        else:

                                            # Decide if needed to run

                                            if "each" in job:
                                                seconds_between_now_and_job_last_run = (now - job_last_run).total_seconds()
                                                logger.info("Job {server}/{job} seconds between now and job last run: {secs}".format(server=server["fqdn"], job=job["id"], secs=seconds_between_now_and_job_last_run))
                                                seconds_needed_to_wait = 0-2*MINUTES_JITTER*60
                                                if "years" in job["each"]:
                                                    seconds_needed_to_wait += 60*60*24*365*job["each"]["years"]
                                                if "months" in job["each"]:
                                                    seconds_needed_to_wait += 60*60*24*31*job["each"]["month"]
                                                if "weeks" in job["each"]:
                                                    seconds_needed_to_wait += 60*60*24*7*job["each"]["weeks"]
                                                if "days" in job["each"]:
                                                    seconds_needed_to_wait += 60*60*24*job["each"]["days"]
                                                if "hours" in job["each"]:
                                                    seconds_needed_to_wait += 60*60*job["each"]["hours"]
                                                if "minutes" in job["each"]:
                                                    seconds_needed_to_wait += 60*job["each"]["minutes"]
                                                logger.info("Job {server}/{job} seconds needed to wait from \"each\" key: {secs}".format(server=server["fqdn"], job=job["id"], secs=seconds_needed_to_wait))
                                                if seconds_between_now_and_job_last_run < seconds_needed_to_wait:
                                                    logger.info("Job {server}/{job} skipped because: {secs1} < {secs2}".format(server=server["fqdn"], job=job["id"], secs1=seconds_between_now_and_job_last_run, secs2=seconds_needed_to_wait))
                                                    continue

                                            if "minutes" in job:
                                                minutes_rewrited = []
                                                for minutes in job["minutes"]:
                                                    if len(str(minutes).split("-")) > 1:
                                                        for m in range(int(str(minutes).split("-")[0]), int(str(minutes).split("-")[1])+1):
                                                            minutes_rewrited.append(m)
                                                    else:
                                                        # Apply MINUTES_JITTER
                                                        for m in range(minutes, minutes + MINUTES_JITTER):
                                                            minutes_rewrited.append(m)
                                                logger.info("Job {server}/{job} should be run on minutes: {mins}".format(server=server["fqdn"], job=job["id"], mins=minutes_rewrited))
                                                now_minute = int(datetime.strftime(now, "%M"))
                                                logger.info("Job {server}/{job} now minute is: {minute}".format(server=server["fqdn"], job=job["id"], minute=now_minute))
                                                if now_minute not in minutes_rewrited:
                                                    logger.info("Job {server}/{job} skipped because now minute is not in run minutes list".format(server=server["fqdn"], job=job["id"]))
                                                    continue

                                            if "hours" in job:
                                                hours_rewrited = []
                                                for hours in job["hours"]:
                                                    if len(str(hours).split("-")) > 1:
                                                        for h in range(int(str(hours).split("-")[0]), int(str(hours).split("-")[1])+1):
                                                            hours_rewrited.append(h)
                                                    else:
                                                        hours_rewrited.append(hours)
                                                logger.info("Job {server}/{job} should be run on hours: {hours}".format(server=server["fqdn"], job=job["id"], hours=hours_rewrited))
                                                now_hour = int(datetime.strftime(now, "%H"))
                                                logger.info("Job {server}/{job} now hour is: {hour}".format(server=server["fqdn"], job=job["id"], hour=now_hour))
                                                if now_hour not in hours_rewrited:
                                                    logger.info("Job {server}/{job} skipped because now hour is not in run hours list".format(server=server["fqdn"], job=job["id"]))
                                                    continue
                                            
                                            if "days" in job:
                                                days_rewrited = []
                                                for days in job["days"]:
                                                    if len(str(days).split("-")) > 1:
                                                        for d in range(int(str(days).split("-")[0]), int(str(days).split("-")[1])+1):
                                                            days_rewrited.append(d)
                                                    else:
                                                        days_rewrited.append(days)
                                                logger.info("Job {server}/{job} should be run on days: {days}".format(server=server["fqdn"], job=job["id"], days=days_rewrited))
                                                now_day = int(datetime.strftime(now, "%d"))
                                                logger.info("Job {server}/{job} now day is: {day}".format(server=server["fqdn"], job=job["id"], day=now_day))
                                                if now_day not in days_rewrited:
                                                    logger.info("Job {server}/{job} skipped because now day is not in run days list".format(server=server["fqdn"], job=job["id"]))
                                                    continue
                                            
                                            if "months" in job:
                                                months_rewrited = []
                                                for months in job["months"]:
                                                    if len(str(months).split("-")) > 1:
                                                        for m in range(int(str(months).split("-")[0]), int(str(months).split("-")[1])+1):
                                                            months_rewrited.append(m)
                                                    else:
                                                        months_rewrited.append(months)
                                                logger.info("Job {server}/{job} should be run on months: {months}".format(server=server["fqdn"], job=job["id"], months=months_rewrited))
                                                now_month = int(datetime.strftime(now, "%m"))
                                                logger.info("Job {server}/{job} now month is: {month}".format(server=server["fqdn"], job=job["id"], month=now_month))
                                                if now_month not in months_rewrited:
                                                    logger.info("Job {server}/{job} skipped because now month is not in run months list".format(server=server["fqdn"], job=job["id"]))
                                                    continue
                                            
                                            if "years" in job:
                                                years_rewrited = []
                                                for years in job["years"]:
                                                    if len(str(years).split("-")) > 1:
                                                        for y in range(int(str(years).split("-")[0]), int(str(years).split("-")[1])+1):
                                                            years_rewrited.append(y)
                                                    else:
                                                        years_rewrited.append(years)
                                                logger.info("Job {server}/{job} should be run on years: {years}".format(server=server["fqdn"], job=job["id"], years=years_rewrited))
                                                now_year = int(datetime.strftime(now, "%Y"))
                                                logger.info("Job {server}/{job} now year is: {year}".format(server=server["fqdn"], job=job["id"], year=now_year))
                                                if now_year not in years_rewrited:
                                                    logger.info("Job {server}/{job} skipped because now year is not in run years list".format(server=server["fqdn"], job=job["id"]))
                                                    continue
                                            
                                            if "weekdays" in job:
                                                logger.info("Job {server}/{job} should be run on weekdays: {weekdays}".format(server=server["fqdn"], job=job["id"], weekdays=job["weekdays"]))
                                                now_weekday = datetime.strftime(now, "%a")
                                                logger.info("Job {server}/{job} now weekday is: {weekday}".format(server=server["fqdn"], job=job["id"], weekday=now_weekday))
                                                if now_weekday not in job["weekdays"]:
                                                    logger.info("Job {server}/{job} skipped because now weekday is not in run weekdays list".format(server=server["fqdn"], job=job["id"]))
                                                    continue

                                        # Run job

                                        if job["type"] == "salt_cmd":
                                            script = textwrap.dedent(
                                                """
                                                .gitlab-server-job/pipeline_salt_cmd.sh nowait {salt_project} {timeout} {server} "{job_cmd}"
                                                """
                                            ).format(salt_project=client_dict["gitlab"]["salt_project"]["path"], timeout=job["timeout"], server=server["fqdn"], job_cmd=job["cmd"])
                                            logger.info("Running bash script:")
                                            logger.info(script)
                                            subprocess.run(script, shell=True, universal_newlines=True, check=True, executable="/bin/bash")
                                        elif job["type"] == "rsnapshot_backup_ssh":
                                            
                                            # Decide which connect host:port to use
                                            if "ssh" in server:

                                                if "host" in server["ssh"]:
                                                    ssh_host = server["ssh"]["host"]
                                                else:
                                                    ssh_host = server["fqdn"]

                                                if "port" in server["ssh"]:
                                                    ssh_port = server["ssh"]["port"]
                                                else:
                                                    ssh_port = "22"

                                            else:

                                                ssh_host = server["fqdn"]
                                                ssh_port = "22"

                                            # Decide ssh jump
                                            if "ssh" in server and "jump" in server["ssh"]:
                                                ssh_jump = "{host}:{port}".format(host=server["ssh"]["jump"]["host"], port=server["ssh"]["jump"]["port"] if "port" in server["ssh"]["jump"] else "22")
                                            else:
                                                ssh_jump = ""

                                            script = textwrap.dedent(
                                                """
                                                .gitlab-server-job/pipeline_rsnapshot_backup.sh nowait {salt_project} 0 {server} SSH {ssh_host} {ssh_port} {ssh_jump}
                                                """
                                            ).format(salt_project=client_dict["gitlab"]["salt_project"]["path"], server=server["fqdn"], ssh_host=ssh_host, ssh_port=ssh_port, ssh_jump=ssh_jump)
                                            logger.info("Running bash script:")
                                            logger.info(script)
                                            subprocess.run(script, shell=True, universal_newlines=True, check=True, executable="/bin/bash")
                                        elif job["type"] == "rsnapshot_backup_salt":
                                            script = textwrap.dedent(
                                                """
                                                .gitlab-server-job/pipeline_rsnapshot_backup.sh nowait {salt_project} {timeout} {server} SALT
                                                """
                                            ).format(salt_project=client_dict["gitlab"]["salt_project"]["path"], timeout=job["timeout"], server=server["fqdn"])
                                            logger.info("Running bash script:")
                                            logger.info(script)
                                            subprocess.run(script, shell=True, universal_newlines=True, check=True, executable="/bin/bash")
                                        else:
                                            raise Exception("Unknown job type: {jtype}".format(jtype=job["type"]))

                                        # Save job last run time per server in history_dict
                                        if server["fqdn"] not in history_dict:
                                            history_dict[server["fqdn"]] = {}
                                        # Save not actual job run time, but this script run time
                                        # Because we need this time to make decision - run or not
                                        # But because each job in loop runs ~5 secs - actual time drifts and diff can go out of window
                                        history_dict[server["fqdn"]][job["id"]] = datetime.strftime(now, "%Y-%m-%d %H:%M:%S %z %Z")
                                        with open(HISTORY_JSON, "w") as history_json:
                                            json.dump(history_dict, history_json)
                                    
                                    except Exception as e:
                                        logger.error("Caught exception, but not interrupting")
                                        logger.exception(e)
                                        errors = True
                
                        except Exception as e:
                            logger.error("Caught exception, but not interrupting")
                            logger.exception(e)
                            errors = True

                except Exception as e:
                    logger.error("Caught exception, but not interrupting")
                    logger.exception(e)
                    errors = True

            # Exit with error if there were errors
            if errors:
                raise Exception("There were errors")

        if args.prune_run_tags:
            
            # Connect to GitLab
            gl = gitlab.Gitlab(acc_yaml_dict["gitlab"]["url"], private_token=GL_ADMIN_PRIVATE_TOKEN)
            gl.auth()

            # For *.yaml in client dir
            for client_file in glob.glob("{0}/{1}".format(CLIENTS_SUBDIR, YAML_GLOB)):

                # Client file errors should not stop other clients
                try:
                
                    logger.info("Found client file: {0}".format(client_file))

                    # Load client YAML
                    client_dict = load_yaml("{0}/{1}".format(WORK_DIR, client_file), logger)
                    if client_dict is None:
                        raise Exception("Config file error or missing: {0}/{1}".format(WORK_DIR, client_file))
                    
                    # Skip other clients
                    prune_client, prune_age = args.prune_run_tags
                    if prune_client != "ALL" and client_dict["name"].lower() != prune_client:
                        continue

                    # Skip disabled clients
                    if not client_dict["active"]:
                        continue

                    # Skip clients without salt_project
                    if "salt_project" not in client_dict["gitlab"]:
                        continue
                    
                    # Skip clients with jobs disabled
                    if "jobs_disabled" in client_dict and client_dict["jobs_disabled"]:
                        continue

                    # Get GitLab project for client
                    project = gl.projects.get(client_dict["gitlab"]["salt_project"]["path"])
                    logger.info("Salt project {project} for client {client} ssh_url_to_repo: {ssh_url_to_repo}, path_with_namespace: {path_with_namespace}".format(project=client_dict["gitlab"]["salt_project"]["path"], client=client_dict["name"], path_with_namespace=project.path_with_namespace, ssh_url_to_repo=project.ssh_url_to_repo))

                    try:
                        # Prune
                        script = textwrap.dedent(
                            """
                            .gitlab-server-job/prune_run_tags.sh {salt_project} {age} git
                            """
                        ).format(salt_project=client_dict["gitlab"]["salt_project"]["path"], age=prune_age)
                        logger.info("Running bash script:")
                        logger.info(script)
                        subprocess.run(script, shell=True, universal_newlines=True, check=True, executable="/bin/bash")
                    except KeyboardInterrupt:
                        # Remove lock coz trap doesn't work if run inside python
                        script = textwrap.dedent(
                            """
                            rm -rf .locks/prune_run_tags.lock
                            """
                        )
                        logger.info("Running bash script:")
                        logger.info(script)
                        subprocess.run(script, shell=True, universal_newlines=True, check=True, executable="/bin/bash")
                        raise

                except Exception as e:
                    logger.error("Caught exception, but not interrupting")
                    logger.exception(e)
                    errors = True
                
            # Exit with error if there were errors
            if errors:
                raise Exception("There were errors")

    # Reroute catched exception to log
    except Exception as e:
        logger.exception(e)
        logger.info("Finished {LOGO} with errors".format(LOGO=LOGO))
        sys.exit(1)

    logger.info("Finished {LOGO}".format(LOGO=LOGO))
