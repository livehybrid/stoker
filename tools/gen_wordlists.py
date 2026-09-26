#!/usr/bin/env python3
"""Regenerate the pack builder's shipped word lists.

Writes ``server/packbuilder/wordlists/<name>.txt`` (one value per line; a value
repeated N times is N times as likely to be picked, which is how the weighted
lists such as ``http_status`` express their mix) and ``index.json`` (titles and
descriptions for the UI). Every value here is authored or generated synthetic
data; nothing is copied from a third-party corpus, so the lists are safe to
ship in this Apache-2.0 repo. Deterministic: the same script run twice writes
the same bytes.

    python3 tools/gen_wordlists.py
"""
from __future__ import annotations

import json
import os
import random

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "packbuilder", "wordlists")

CITIES = """London Manchester Birmingham Leeds Glasgow Edinburgh Bristol Liverpool Sheffield Cardiff Belfast Newcastle
Nottingham Leicester Southampton Brighton Oxford Cambridge York Aberdeen Dublin Paris Lyon Marseille Berlin Munich
Hamburg Frankfurt Amsterdam Rotterdam Brussels Antwerp Madrid Barcelona Valencia Lisbon Porto Rome Milan Naples Turin
Vienna Zurich Geneva Copenhagen Stockholm Oslo Helsinki Warsaw Krakow Prague Budapest Athens Istanbul Bucharest Sofia
Reykjavik Tallinn Riga Vilnius Kyiv New_York Los_Angeles Chicago Houston Phoenix Philadelphia San_Antonio San_Diego
Dallas Austin Seattle Denver Boston Atlanta Miami Portland Toronto Montreal Vancouver Calgary Ottawa Mexico_City
Guadalajara Monterrey Bogota Lima Santiago Buenos_Aires Sao_Paulo Rio_de_Janeiro Brasilia Caracas Quito Montevideo
Tokyo Osaka Kyoto Seoul Busan Beijing Shanghai Shenzhen Guangzhou Hong_Kong Taipei Singapore Kuala_Lumpur Bangkok
Jakarta Manila Hanoi Ho_Chi_Minh_City Mumbai Delhi Bengaluru Chennai Hyderabad Kolkata Karachi Lahore Dhaka Colombo
Dubai Abu_Dhabi Doha Riyadh Tel_Aviv Cairo Casablanca Lagos Nairobi Accra Addis_Ababa Johannesburg Cape_Town Durban
Sydney Melbourne Brisbane Perth Adelaide Auckland Wellington Christchurch""".split()

COUNTRIES = """United_Kingdom Ireland France Germany Netherlands Belgium Spain Portugal Italy Austria Switzerland
Denmark Sweden Norway Finland Poland Czechia Hungary Greece Turkey Romania Bulgaria Iceland Estonia Latvia Lithuania
Ukraine United_States Canada Mexico Colombia Peru Chile Argentina Brazil Uruguay Japan South_Korea China Taiwan
Singapore Malaysia Thailand Indonesia Philippines Vietnam India Pakistan Bangladesh Sri_Lanka United_Arab_Emirates
Qatar Saudi_Arabia Israel Egypt Morocco Nigeria Kenya Ghana Ethiopia South_Africa Australia New_Zealand""".split()

COUNTRY_CODES = """GB IE FR DE NL BE ES PT IT AT CH DK SE NO FI PL CZ HU GR TR RO BG IS EE LV LT UA US CA MX CO PE CL AR
BR UY JP KR CN TW SG MY TH ID PH VN IN PK BD LK AE QA SA IL EG MA NG KE GH ET ZA AU NZ""".split()

FIRST_NAMES = """Oliver George Arthur Noah Muhammad Leo Harry Oscar Archie Henry Theodore Freddie Jack Charlie Theo
Alfie Jacob Thomas Finley Arlo William Lucas Roman Tommy Isaac Teddy Alexander Luca Edward James Joshua Albie Elijah
Max Mohammed Reuben Mason Sebastian Rory Jude Olivia Amelia Isla Ava Ivy Freya Lily Florence Mia Willow Rosie Sophia
Isabella Grace Daisy Sienna Poppy Elsie Emily Ella Evelyn Phoebe Sofia Evie Charlotte Harper Millie Matilda Maya
Sophie Alice Emilia Isabelle Ruby Luna Maisie Aria Penelope Mila Bonnie Eva Hallie Eliza Ada Violet Esme Arabella
Imogen Jessica Delilah Clara Priya Aarav Ananya Arjun Diya Rohan Chen Wei Mei Hiroshi Yuki Sakura Kenji Min-jun
Seo-yeon Ji-woo Carlos Sofia Mateo Valentina Diego Camila Lucia Hugo Chloe Louis Emma Lukas Hannah Jonas Lena Finn
Mariam Omar Fatima Yusuf Aisha Ibrahim Zainab Amara Kofi Ama Chinedu Ngozi Thabo Lerato Liam Noa Sean Aoife Ciaran
Niamh Magnus Astrid Lars Ingrid Mikko Aino Piotr Zofia Tomas Eliska Dimitri Eleni""".split()

LAST_NAMES = """Smith Jones Taylor Brown Williams Wilson Johnson Davies Patel Robinson Wright Thompson Evans Walker
White Roberts Green Hall Thomas Clarke Jackson Wood Harris Edwards Turner Martin Cooper Hill Ward Hughes Moore Clark
King Harrison Lewis Baker Lee Allen Morris Khan Scott Watson Davis Parker James Bennett Young Phillips Richardson
Mitchell Bailey Carter Cook Singh Shaw Bell Collins Morgan Kelly Begum Miller Cox Hussain Marshall Simpson Price
Anderson Adams Wilkinson Ali Ahmed Foster Ellis Murphy Chapman Mason Gray Richards Webb Griffiths Hunt Palmer
Campbell Holmes Mills Rogers Barnes Knight Matthews Barker Powell Stevens Kaur Fisher Butler Dixon Russell Harvey
Pearson Graham Fletcher Reid Nguyen Tanaka Suzuki Sato Kim Park Wang Zhang Liu Gonzalez Rodriguez Fernandez Silva
Santos Rossi Ferrari Muller Schmidt Schneider Dubois Martin Bernard Jansen Janssen Novak Kowalski Nielsen Hansen
Johansson Larsen Virtanen O'Brien Murray Okafor Mensah Dlamini Cohen Levi Haddad Nasser Rahman Das Iyer Reddy""".split()

EMAIL_DOMAINS = "example.com example.org example.net corp.example.com mail.example.com contoso.example fabrikam.example acme.example".split()

DEPARTMENTS = """Finance Engineering Sales Marketing Operations Human_Resources Legal Procurement Customer_Support IT
Security Research Product Facilities Logistics Compliance Data_Science Design Training Executive""".split()

HOST_ROLES = "web app api db cache mq auth search batch worker proxy lb dns mail files vpn build log mon".split()

PROCESS_NAMES = """sshd systemd cron nginx httpd java python3 node postgres mysqld redis-server dockerd containerd
kubelet splunkd rsyslogd auditd sudo bash sh curl wget chronyd snmpd explorer.exe svchost.exe lsass.exe
services.exe winlogon.exe csrss.exe smss.exe taskhostw.exe powershell.exe cmd.exe msedge.exe chrome.exe firefox.exe
outlook.exe teams.exe onedrive.exe notepad.exe rundll32.exe regsvr32.exe wmiprvse.exe spoolsv.exe conhost.exe
dllhost.exe msiexec.exe schtasks.exe net.exe whoami.exe""".split()

HTTP_METHODS = ["GET"] * 70 + ["POST"] * 20 + ["PUT"] * 4 + ["DELETE"] * 2 + ["HEAD"] * 2 + ["PATCH"] + ["OPTIONS"]

HTTP_STATUS = (["200"] * 70 + ["201"] * 3 + ["204"] * 3 + ["301"] * 3 + ["302"] * 4 + ["304"] * 5 + ["400"] * 2
               + ["401"] * 2 + ["403"] * 2 + ["404"] * 4 + ["429"] + ["500"] + ["502"] + ["503"])

URI_PATHS = """/ /index.html /login /logout /account /account/settings /cart /checkout /checkout/confirm /search
/products /products/1001 /products/1002 /products/2040 /products/3107 /category/electronics /category/books
/category/garden /api/v1/orders /api/v1/orders/8812 /api/v1/users /api/v1/users/me /api/v1/health /api/v1/metrics
/api/v2/search /api/v2/recommendations /static/css/site.css /static/js/app.js /static/js/vendor.js
/static/img/logo.png /static/img/hero.jpg /favicon.ico /robots.txt /sitemap.xml /help /help/returns /contact
/blog /blog/2026/release-notes /downloads/report.pdf /wp-login.php /.env /admin /admin/login /graphql
/oauth/authorize /oauth/token /.well-known/openid-configuration""".split()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "curl/8.9.1",
    "python-requests/2.32.3",
    "Go-http-client/2.0",
    "okhttp/4.12.0",
    "PostmanRuntime/7.41.2",
]

ACTIONS = ["allowed"] * 12 + ["blocked"] * 3 + ["dropped"] * 2 + ["reset"] + ["alerted"]
SEVERITIES = ["informational"] * 10 + ["low"] * 5 + ["medium"] * 3 + ["high"] * 2 + ["critical"]
LOG_LEVELS = ["INFO"] * 20 + ["DEBUG"] * 6 + ["WARN"] * 4 + ["ERROR"] * 2 + ["FATAL"]
AWS_REGIONS = """us-east-1 us-east-2 us-west-1 us-west-2 eu-west-1 eu-west-2 eu-west-3 eu-central-1 eu-north-1
eu-south-1 ap-southeast-1 ap-southeast-2 ap-northeast-1 ap-northeast-2 ap-south-1 ca-central-1 sa-east-1
me-south-1 af-south-1""".split()
PROTOCOLS = ["TCP"] * 12 + ["UDP"] * 6 + ["ICMP"] * 2 + ["GRE"]
PORTS = ["443"] * 30 + ["80"] * 12 + ["22"] * 6 + ["53"] * 8 + ["3389"] * 3 + ["25"] * 2 + ["123"] * 2 + \
    ["8080", "8443", "3306", "5432", "6379", "9200", "1433", "445", "389", "636", "161", "514", "8088", "9997"]
AUTH_RESULTS = ["success"] * 14 + ["failure"] * 4 + ["locked_out"] + ["mfa_required"]
FILE_EXTENSIONS = ".pdf .docx .xlsx .pptx .txt .csv .zip .png .jpg .log .json .xml .exe .dll .ps1 .sh .py".split()


def _clean(values):
    return [v.replace("_", " ") for v in values]


def _write(name, values):
    path = os.path.join(OUT, name + ".txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(values) + "\n")


def main():
    os.makedirs(OUT, exist_ok=True)
    rng = random.Random(20260926)

    first = _clean(FIRST_NAMES)
    last = LAST_NAMES
    usernames = sorted({("%s.%s" % (f, l)).lower().replace("'", "").replace(" ", "")
                        for f, l in ((rng.choice(first), rng.choice(last)) for _ in range(400))})[:300]
    usernames += ["admin", "administrator", "root", "svc_backup", "svc_splunk", "svc_deploy", "guest", "helpdesk"]
    emails = ["%s@%s" % (u, rng.choice(EMAIL_DOMAINS)) for u in usernames[:250]]
    full_names = sorted({"%s %s" % (rng.choice(first), rng.choice(last)) for _ in range(400)})[:300]
    hostnames = sorted({"%s-%s-%02d" % (rng.choice(HOST_ROLES), rng.choice(["prd", "prd", "stg", "dev"]),
                                        rng.randint(1, 24)) for _ in range(400)})[:200]
    workstations = sorted({"WS-%s-%04d" % (rng.choice(["LON", "MAN", "LDS", "NYC", "SIN", "SYD"]), rng.randint(1, 9999))
                           for _ in range(200)})[:150]
    internal_ips = sorted({"10.%d.%d.%d" % (rng.choice([0, 1, 2, 10, 20, 30]), rng.randint(0, 255), rng.randint(1, 254))
                           for _ in range(300)} |
                          {"192.168.%d.%d" % (rng.choice([0, 1, 10, 100]), rng.randint(1, 254)) for _ in range(100)} |
                          {"172.16.%d.%d" % (rng.randint(0, 31), rng.randint(1, 254)) for _ in range(100)})
    # Documentation ranges (RFC 5737) plus generated addresses that avoid the
    # private, loopback and link-local blocks: "external" without naming anyone.
    external_ips = ["192.0.2.%d" % i for i in range(1, 255, 3)] + \
        ["198.51.100.%d" % i for i in range(1, 255, 3)] + ["203.0.113.%d" % i for i in range(1, 255, 3)]
    while len(external_ips) < 500:
        a = rng.randint(1, 223)
        if a in (10, 127, 169, 172, 192):
            continue
        external_ips.append("%d.%d.%d.%d" % (a, rng.randint(0, 255), rng.randint(0, 255), rng.randint(1, 254)))
    mac_addresses = sorted({":".join("%02x" % rng.randint(0, 255) for _ in range(6)) for _ in range(150)})

    lists = [
        ("cities", "Cities", "World cities (spaces kept, e.g. \"New York\")", _clean(CITIES)),
        ("countries", "Countries", "Country names", _clean(COUNTRIES)),
        ("country_codes", "Country codes", "ISO 3166 alpha-2 codes", COUNTRY_CODES),
        ("first_names", "First names", "Given names", first),
        ("last_names", "Last names", "Family names", last),
        ("full_names", "Full names", "\"First Last\" pairs", full_names),
        ("usernames", "Usernames", "first.last style accounts plus service accounts", usernames),
        ("emails", "Email addresses", "Addresses on example domains", emails),
        ("departments", "Departments", "Organisation departments", _clean(DEPARTMENTS)),
        ("hostnames", "Server hostnames", "role-env-NN names (web-prd-03)", hostnames),
        ("workstations", "Workstations", "WS-SITE-NNNN names", workstations),
        ("internal_ips", "Internal IPs", "RFC 1918 addresses (10/8, 172.16/12, 192.168/16)", internal_ips),
        ("external_ips", "External IPs", "Documentation ranges and generated public-looking addresses", external_ips),
        ("mac_addresses", "MAC addresses", "Colon-separated lower-case", mac_addresses),
        ("http_methods", "HTTP methods", "Weighted: mostly GET, some POST", HTTP_METHODS),
        ("http_status", "HTTP status codes", "Weighted: mostly 200, a few 3xx/4xx/5xx", HTTP_STATUS),
        ("uri_paths", "URI paths", "Web and API paths incl. a few probes", URI_PATHS),
        ("user_agents", "User agents", "Browsers, mobiles, bots and API clients", USER_AGENTS),
        ("process_names", "Process names", "Linux and Windows processes", PROCESS_NAMES),
        ("actions", "Actions", "Firewall-style outcomes, mostly allowed", ACTIONS),
        ("severities", "Severities", "informational to critical, weighted", SEVERITIES),
        ("log_levels", "Log levels", "INFO/DEBUG/WARN/ERROR/FATAL, weighted", LOG_LEVELS),
        ("aws_regions", "AWS regions", "Region codes", AWS_REGIONS),
        ("protocols", "Network protocols", "TCP/UDP/ICMP/GRE, weighted", PROTOCOLS),
        ("ports", "Common ports", "Weighted towards 443/80/53/22", PORTS),
        ("auth_results", "Auth results", "success/failure/locked_out/mfa_required", AUTH_RESULTS),
        ("file_extensions", "File extensions", "Documents, archives, binaries, scripts", FILE_EXTENSIONS),
    ]
    index = []
    for name, title, description, values in lists:
        _write(name, values)
        index.append({"name": name, "title": title, "description": description})
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
        fh.write("\n")
    print("wrote %d lists to %s" % (len(lists), os.path.normpath(OUT)))


if __name__ == "__main__":
    main()
