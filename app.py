from flask import Flask, request
from botocore.client import Config
import ansible_runner
import shutil
import boto3
import os
import datetime
import json
import psycopg2
import logging
import paramiko
import string
import random
from werkzeug.utils import secure_filename
import subprocess

CURR_PATH = os.getcwd()
tomcat_service_file = f"{CURR_PATH}/ansible/tomcat.service"

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

conn = psycopg2.connect(
    host="127.0.0.1",
    database="ansible",
    user=os.environ["DB_USERNAME"],
    password=os.environ["DB_PASSWORD"],
)
s3 = boto3.client(
    "s3",
    aws_access_key_id=os.environ["AWS_ACCESS_KEY"].strip(),
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"].strip(),
    region_name=os.environ["AWS_REGION"],
    config=Config(signature_version="s3v4"),
)

db = conn.cursor()

try:
    db.execute(
        "CREATE TABLE IF NOT EXISTS logs (id serial PRIMARY KEY, host varchar (32)NOT NULL, date_time timestamptz NOT NULL, log_file_name varchar(512));"
    )
    conn.commit()

except Exception as e:
    logger.error(e)

app = Flask(__name__)


def getRandomString(N):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=N))


def searchInFile(file_path, search_string):
    with open(file_path) as f:
        datafile = f.readlines()
    for line in datafile:
        if search_string in line:
            return True
    return False


def addEntryToDB(host, date_time, log_file_link):
    try:
        sql = "INSERT INTO logs(host, date_time, log_file_name) VALUES (%s, %s, %s)"
        db.execute(sql, (host, date_time, log_file_link))
        conn.commit()
    except Exception as e:
        logger.error(e)


def saveLogFileToS3(log_file_path, name, acl="authenticated-read"):
    filename = secure_filename(getRandomString(6) + "-" + name)
    try:
        with open(log_file_path, "rb") as file:
            s3.upload_fileobj(
                file,
                os.environ["AWS_BUCKET_NAME"].strip(),
                filename,
                ExtraArgs={"ACL": acl, "ContentType": "text/plain"},
            )
        return filename
    except Exception as e:
        logger.error(e)
        return ""


def getLogFileLink(fileName):
    return s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["AWS_BUCKET_NAME"].strip(),
            "Key": fileName.strip(),
        },
        ExpiresIn=3600,
    )


def getWarFileName(url):
    try:
        parts = url.split("/")
        file_name = parts[-1].split(".war")[0]
        return file_name
    except Exception as e:
        logger.error(e)
        return ""


def getNginxConf(hostname, warfile):
    conf = """
    server {
    listen 80 default_server;
    server_name %s;

    location / {
        proxy_pass http://localhost:8080/%s/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
    """ % (
        hostname,
        warfile,
    )
    return conf


@app.route("/")
def hello_world():
    return "Service is up and running."


@app.route("/update-hosts", methods=["POST"])
def update_known_hosts():
    try:
        host = request.json["host"]
        subprocess.run(["ssh-keygen", "-R", host], check=True)
        output = subprocess.check_output(["ssh-keyscan", "-t", "rsa", host])
        with open(os.path.expanduser("~/.ssh/known_hosts"), "ab") as file:
            file.write(output)
        return "Host Updated Successfully"
    except Exception as e:
        logger.error(e)
        return str(e)


@app.route("/logs", methods=["POST"])
def getLogs():
    body = request.json
    if not "host" in body:
        return "[host] is needed (IP Address of server).", 400
    if not "password" in body:
        return "[password] is needed of root user.", 400
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=body["host"], username="root", password=body["password"]
        )
        curr = conn.cursor()
        curr.execute(
            "SELECT * from logs where host='%s' order by date_time desc"
            % (body["host"])
        )
        logs = curr.fetchall()
        curr.close()
        logs_json = []
        for log in logs:
            logs_json.append(
                {
                    "Date-Time": datetime.datetime.fromisoformat(str(log[2])).strftime(
                        "%A, %d %B %Y %I:%M %p"
                    ),
                    "Log File": getLogFileLink(log[3]),
                }
            )
        return json.dumps(logs_json)
    except paramiko.AuthenticationException:
        return "You are not authorized. Please check your credentials.", 400
    except Exception as e:
        logger.error(e)
        return "Internal Server Error.", 500


@app.route("/deploy", methods=["POST"])
def deploy():
    dateTime = str(datetime.datetime.now().isoformat())
    curr_execution = ""
    try:
        body = request.json
        if not "host" in body:
            return "[host] is needed (IP Address of server).", 400
        if not "password" in body:
            return "[password] is needed of root user.", 400
        if not "war" in body:
            return "[war] is needed (War file to be deployed).", 400

        curr_execution = (
            f'{CURR_PATH}/executions/{body["host"]}/{dateTime}-{getRandomString(6)}'
        )
        os.makedirs(curr_execution)

        nginx_conf_file = f"{curr_execution}/nginx.conf"
        with open(nginx_conf_file, "w") as file:
            file.write(getNginxConf(body["host"], getWarFileName(body["war"])))

        vars = {
            "ansible_ssh_pass": body["password"],
            "host": body["host"],
            "war_file_link": body["war"],
            "tomcat_service_file": tomcat_service_file,
            "nginx_conf_file": nginx_conf_file,
            "war_file_name": getWarFileName(body["war"]),
        }

        if vars["war_file_name"] == "":
            return "[war] should be war file link similar to https://github.com/raunit-verma/war/raw/main/devtron.war"

        # copy ansible playbook
        shutil.copy(f"{CURR_PATH}/ansible/deploy_war.yaml", curr_execution)

        # create hosts.ini file
        hosts_file = f"{curr_execution}/hosts.ini"
        with open(hosts_file, "w") as file:
            file.write(body["host"])

        # create ansible.cfg
        ansible_cfg = f"{curr_execution}/ansible.cfg"

        with open(ansible_cfg, "w") as file:
            file.write(f"[defaults]\nlog_path={curr_execution}/ansible.log")

        # create logs file
        with open(f"{curr_execution}/ansible.log", "w") as file:
            pass

        r = ansible_runner.run(
            private_data_dir=curr_execution,
            playbook=f"{curr_execution}/deploy_war.yaml",
            inventory=f"{curr_execution}/hosts.ini",
            extravars=vars,
            verbosity=2,
            quiet=True,
        )

        if r.status == "successful":
            return "Completed Successfully"
        if searchInFile(f"{curr_execution}/ansible.log", "incorrect password"):
            return "Invalid/incorrect password"
        elif searchInFile(f"{curr_execution}/ansible.log", "Invalid archive"):
            return "Tomcat archive link expired. Please contact admin."
        elif searchInFile(f"{curr_execution}/ansible.log", "HTTP Error 404: Not Found"):
            return "WAR file not found. HTTP Error"
        return "Process failed. Please see the logs to debug."
    except Exception as e:
        logger.error(e)
        return "Internal Server Error", 500
    finally:
        fileName = saveLogFileToS3(
            f"{curr_execution}/ansible.log",
            ("%s-%s-ansible.log" % (body["host"], dateTime)),
        )
        addEntryToDB(body["host"], dateTime, fileName)
        shutil.rmtree(curr_execution)


if __name__ == "__main__":
    app.run()
