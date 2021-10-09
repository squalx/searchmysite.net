import json
from urllib.request import urlopen
import psycopg2
import psycopg2.extras
import tldextract
import logging
import re
import httplib2
from bs4 import BeautifulSoup, SoupStrainer
from common import config

solr_url = config.SOLR_URL
solr_query_to_get_indexed_outlinks = "select?q=*%3A*&fq=indexed_outlinks%3A*{}*&fl=url,indexed_outlinks&rows=10000"

db_password = config.DB_PASSWORD
db_name = config.DB_NAME
db_user = config.DB_USER
db_host = config.DB_HOST

domains_sql = "SELECT DISTINCT domain FROM tblIndexedDomains;"
domains_allowing_subdomains_sql = "SELECT setting_value FROM tblSettings WHERE setting_name = 'domain_allowing_subdomains';"
update_indexing_status_sql = "UPDATE tblIndexedDomains "\
    "SET indexing_current_status = (%s), indexing_status_last_updated = now() "\
    "WHERE domain = (%s); "\
    "INSERT INTO tblIndexingLog (domain, status, timestamp, message) "\
    "VALUES ((%s), (%s), now(), (%s));"
indexing_log_sql = "SELECT * FROM tblIndexingLog WHERE domain = (%s) AND status = 'COMPLETE' ORDER BY timestamp DESC LIMIT 1;"


# Get all domains
def get_all_domains():
    domains = []
    try:
        conn = psycopg2.connect(host=db_host, dbname=db_name, user=db_user, password=db_password)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute(domains_sql)
        dd = cursor.fetchall()
        for [d] in dd:
            domains.append(d)
    except psycopg2.Error as e:
        logger = logging.getLogger()
        logger.error('get_all_domains: {}'.format(e.pgerror))
    finally:
        conn.close()
    return domains


# Get the domains which allow subdomains, e.g. github.io 
def get_domains_allowing_subdomains():
    domains_allowing_subdomains = []
    try:
        conn = psycopg2.connect(host=db_host, dbname=db_name, user=db_user, password=db_password)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute(domains_allowing_subdomains_sql)
        domains_allowing_subdomains_results = cursor.fetchall()
        for domains_allowing_subdomain in domains_allowing_subdomains_results:
            domains_allowing_subdomains.append(domains_allowing_subdomain['setting_value'])
    except psycopg2.Error as e:
        logger = logging.getLogger()
        logger.error('get_domains_allowing_subdomains: {}'.format(e.pgerror))
    finally:
        conn.close()
    return domains_allowing_subdomains


# Update indexing log
# status either RUNNING or COMPLETE
def update_indexing_log(domain, status, message):
    domains_allowing_subdomains = []
    try:
        conn = psycopg2.connect(host=db_host, dbname=db_name, user=db_user, password=db_password)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute(update_indexing_status_sql, (status, domain, domain, status, message,))
        conn.commit()
    except psycopg2.Error as e:
        logger = logging.getLogger()
        logger.error('update_indexing_log: {}'.format(e.pgerror))
    finally:
        conn.close()
    return domains_allowing_subdomains
 

# Get latest completed import date
# Assumes the log status is "COMPLETE" and message of the format "Using export: 20210927"
def get_latest_completed_import(domain):
    completed_import_date = ""
    try:
        conn = psycopg2.connect(host=db_host, dbname=db_name, user=db_user, password=db_password)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute(indexing_log_sql, (domain, ))
        result = cursor.fetchone()
        if result and result['message']:
            r = re.search('Using export: (.*)', result['message'])
            if r:
                completed_import_date = r.group(1)
        conn.commit()
    except psycopg2.Error as e:
        logger = logging.getLogger()
        logger.error('update_indexing_log: {}'.format(e.pgerror))
    finally:
        conn.close()
    return completed_import_date


# Get latest available wikipedia export
def get_latest_available_wikipedia_export(dump_location):
    latest_available_wikipedia_export = ""
    exports = []
    http = httplib2.Http()
    status, response = http.request(dump_location)
    for link in BeautifulSoup(response, parse_only=SoupStrainer('a'), features="lxml"):
        if link.has_attr('href'):
            if link['href'][:8].isdigit():
                exports.append(link['href'][:8])
    sorted_exports = sorted(exports)
    return sorted_exports[-1]


# Extract domain from a URL, where domain could be a subdomain if that domain allows subdomains
# This is a variant of ../../web/content/dynamic/admin/util.py which takes domains_allowing_subdomains as an input parameter
# because don't want a database lookup every time this is called in this context
def extract_domain_from_url(url, domains_allowing_subdomains):
    tld = tldextract.extract(url) # returns [subdomain, domain, suffix]
    domain = '.'.join(tld[1:]) if tld[2] != '' else tld[1] # if suffix empty, e.g. localhost, just use domain
    domain = domain.lower() # lowercase the domain to help prevent duplicates
    # Add subdomain if in domains_allowing_subdomains
    if domain in domains_allowing_subdomains: # special domains where a site can be on a subdomain
        if tld[0] and tld[0] != "":
            domain = tld[0] + "." + domain
    return domain


# Logic for generating all the indexed_inlinks for a domain:
# Step 1:
# Search for any indexed_outlinks to that domain, i.e.
# /solr/content/select?q=*%3A*&fq=indexed_outlinks%3A*{domain}*&fl=url,indexed_outlinks&rows=10000
# This will return urls each with a list of indexed_outlinks to that domain (and potentially other domains).
# Note that it doesn't appear possible to restrict indexed_outlinks to just the domain specified in fq=indexed_outlinks%3A*{domain}*
# (see https://issues.apache.org/jira/browse/SOLR-3955) so other domains will need to be filtered out later.
# Step 2:
# Invert, so instead of a dict of indexed links each with a list of indexed_outlinks 
# it is a dict of indexed_outlinks each with a list of indexed links.
# The indexed_outlinks, if matching the domain, will be the ones that will have indexed_inlinks value set for them, and the value of the
# indexed_inlinks will be the list of urls.
def get_all_indexed_inlinks_for_domain(domain):
    indexed_inlinks = {}
    solrquery = solr_query_to_get_indexed_outlinks.format(domain)
    connection = urlopen(solr_url + solrquery)
    results = json.load(connection)
    if results['response']['docs']:
        for doc in results['response']['docs']:
            url = doc['url']
            indexed_outlinks = doc['indexed_outlinks']
            for indexed_outlink in indexed_outlinks:
                if domain in indexed_outlink:
                    if indexed_outlink not in indexed_inlinks:
                        indexed_inlinks[indexed_outlink] = [url]
                    else:
                        indexed_inlinks[indexed_outlink].append(url)
    return indexed_inlinks