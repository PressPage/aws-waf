import json
import boto3
import datetime
import time
import math
from elasticsearch import Elasticsearch
import warnings
from slacker import Slacker
import os

warnings.filterwarnings('ignore')

print('Loading function')

#======================================================================================================================
# Constants
#======================================================================================================================
# Configurables
OUTPUT_BUCKET = None
IP_SET_ID_MANUAL_BLOCK = None
IP_SET_ID_AUTO_BLOCK = None
IP_SET_ID_AUTO_COUNT = None

BLACKLIST_BLOCK_PERIOD = None # in minutes
BLACKLIST_COUNT_PERIOD = None # in minutes
REQUEST_PER_MINUTE_LIMIT = None

ES_HOST = None
ES_PORT = None

LIMIT_IP_ADDRESS_RANGES_PER_IP_MATCH_CONDITION = 1000
API_CALL_NUM_RETRIES = 3

OUTPUT_FILE_NAME = 'current_outstanding_requesters.json'


def get_elastic_outstanding_requesters():
    print '[get_elastic_outstanding_requesters] Start'

    outstanding_requesters = {}
    outstanding_requesters['block'] = {}
    outstanding_requesters['count'] = {}
    num_requests = 0
    result = {}
    try:
        # --------------------------------------------------------------------------------------------------------------
        print '[get_elastic_outstanding_requesters] \tinitiate ElasticSearch'
        # --------------------------------------------------------------------------------------------------------------
        es = Elasticsearch(
            hosts=[{'host': ES_HOST, 'port': ES_PORT}],
            use_ssl=True,
            verify_certs=False
        )
        #--------------------------------------------------------------------------------------------------------------
        print '[get_elastic_outstanding_requesters] \tRequest ElasticSearch'
        #--------------------------------------------------------------------------------------------------------------
        body2 = {
            "size": 0,
            "aggs": {
                "last2minute": {
                    "filter": {"bool": {
                        "must": [
                            {"range": {"datetime": {"gte": "now-2m", "lte": "now"}}}
                        ],
                        "must_not": [
                            {"regexp": {"user-agent": ".*(gsa-crawler).*"}},
                            {"regexp": {"user-agent": ".*(ELB-HealthChecker).*"}},
                            {"regexp": {"user-agent": ".*(Amazon CloudFront).*"}},
                            {"regexp": {"request": ".*(\.css).*"}},
                            {"regexp": {"request": ".*(\.js).*"}}
                        ]
                    }},
                    "aggs": {
                        "group_by_state": {
                            "terms": {
                                "field": "x-forwarded-for"
                            }
                        }
                    }
                }
            }
        }

        response = es.search(
            index='logmonitor-' + datetime.datetime.now().strftime("%Y-%m-%d"),
            body=body2
        )

        #--------------------------------------------------------------------------------------------------------------
        print '[get_elastic_outstanding_requesters] \tHandle ES response'
        #--------------------------------------------------------------------------------------------------------------
        num_requests = response['aggregations']['last2minute']['group_by_state']['sum_other_doc_count']
        for ip_array in response['aggregations']['last2minute']['group_by_state']['buckets']:
            ipx = ip_array['key']
            if ipx.find(',') > 0:
                ipx = ipx.split(',')[0]
                if ipx.find(':') > 0:
                    ipx = ipx.split(':')[0]
            result[ipx] = ip_array['doc_count']

        #--------------------------------------------------------------------------------------------------------------
        print '[get_elastic_outstanding_requesters] \tKeep only outstanding requesters'
        #--------------------------------------------------------------------------------------------------------------
        now_timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for k, v in result.iteritems():
            if v > REQUEST_PER_MINUTE_LIMIT:
                if k not in outstanding_requesters['block'].keys() or outstanding_requesters['block'][k] < v:
                    outstanding_requesters['block'][k] = {'max_req_per_min': v, 'updated_at': now_timestamp_str}

    except Exception, e:
        print "[get_elastic_outstanding_requesters] \tError to read data from ElasticSearch: %s" % e.message

    print '[get_elastic_outstanding_requesters] End'
    return outstanding_requesters, num_requests


def merge_current_blocked_requesters(key_name, outstanding_requesters):
    print "[merge_current_blocked_requesters] Start"

    try:
        now_timestamp = datetime.datetime.now()
        now_timestamp_str = now_timestamp.strftime("%Y-%m-%d %H:%M:%S")
        remote_outstanding_requesters = {}

        #--------------------------------------------------------------------------------------------------------------
        print "[merge_current_blocked_requesters] \tDownload current blocked IPs"
        #--------------------------------------------------------------------------------------------------------------
        try:
            local_file_path = '/tmp/' + key_name.split('/')[-1] + '_REMOTE.json'
            s3 = boto3.client('s3')
            s3.download_file(OUTPUT_BUCKET, OUTPUT_FILE_NAME, local_file_path)

            with open(local_file_path, 'r') as file_content:
                remote_outstanding_requesters = json.loads(file_content.read())

        except Exception, e:
            print "[merge_current_blocked_requesters] \t\tFailed to download current blocked IPs"

        #--------------------------------------------------------------------------------------------------------------
        print "[merge_current_blocked_requesters] \tExpire Block IP rules"
        #--------------------------------------------------------------------------------------------------------------
        for k, v in remote_outstanding_requesters['block'].iteritems():
            if v['max_req_per_min'] > REQUEST_PER_MINUTE_LIMIT:
                if k in outstanding_requesters['block'].keys():
                    print "[merge_current_blocked_requesters] \t\tUpdating data of BLOCK %s rule"%k
                    max_v = v['max_req_per_min']
                    if outstanding_requesters['block'][k]['max_req_per_min'] > max_v:
                        max_v = outstanding_requesters['block'][k]['max_req_per_min']
                    outstanding_requesters['block'][k] = { 'max_req_per_min': max_v, 'updated_at': now_timestamp_str }
                else:
                    prev_updated_at = datetime.datetime.strptime(v['updated_at'], "%Y-%m-%d %H:%M:%S")
                    total_diff_min = ((now_timestamp - prev_updated_at).total_seconds())/60
                    if total_diff_min > (BLACKLIST_BLOCK_PERIOD + BLACKLIST_COUNT_PERIOD):
                        print "[merge_current_blocked_requesters] \t\tExpired BLOCK and COUNT %s rule"%k
                    elif total_diff_min > (BLACKLIST_BLOCK_PERIOD):
                        print "[merge_current_blocked_requesters] \t\tExpired BLOCK %s rule"%k
                        outstanding_requesters['count'][k] = v
                    else:
                        print "[merge_current_blocked_requesters] \t\tKeeping data of BLOCK %s rule"%k
                        outstanding_requesters['block'][k] = v

        #--------------------------------------------------------------------------------------------------------------
        print "[merge_current_blocked_requesters] \tExpire Count IP rules"
        #--------------------------------------------------------------------------------------------------------------
        for k, v in remote_outstanding_requesters['count'].iteritems():
            if v['max_req_per_min'] > REQUEST_PER_MINUTE_LIMIT:
                if k in outstanding_requesters['block'].keys():
                    print "[merge_current_blocked_requesters] \t\tUpdating data of COUNT %s rule"%k
                    max_v = v['max_req_per_min']
                    if outstanding_requesters['block'][k]['max_req_per_min'] > max_v:
                        max_v = outstanding_requesters['block'][k]['max_req_per_min']
                    outstanding_requesters['block'][k] = { 'max_req_per_min': max_v, 'updated_at': now_timestamp_str }
                else:
                    prev_updated_at = datetime.datetime.strptime(v['updated_at'], "%Y-%m-%d %H:%M:%S")
                    total_diff_min = ((now_timestamp - prev_updated_at).total_seconds())/60
                    if total_diff_min > (BLACKLIST_BLOCK_PERIOD + BLACKLIST_COUNT_PERIOD):
                        print "[merge_current_blocked_requesters] \t\tExpired COUNT %s rule"%k
                    else:
                        print "[merge_current_blocked_requesters] \t\tKeeping data of COUNT %s rule"%k
                        outstanding_requesters['count'][k] = v

    except Exception, e:
        print "[merge_current_blocked_requesters] \tError to merging data"

    print "[merge_current_blocked_requesters] End"
    return outstanding_requesters


def write_output(key_name, outstanding_requesters):
    print "[write_output] Start"

    try:
        current_data = '/tmp/' + key_name.split('/')[-1] + '_LOCAL.json'
        with open(current_data, 'w') as outfile:
            json.dump(outstanding_requesters, outfile)

        s3 = boto3.client('s3')
        s3.upload_file(current_data, OUTPUT_BUCKET, OUTPUT_FILE_NAME, ExtraArgs={'ContentType': "application/json"})

    except Exception, e:
        print "[write_output] \tError to write output file"

    print "[write_output] End"


def waf_get_ip_set(ip_set_id):
    response = None
    waf = boto3.client('waf-regional')

    for attempt in range(API_CALL_NUM_RETRIES):
        try:
            response = waf.get_ip_set(IPSetId=ip_set_id)
        except Exception, e:
            print e
            delay = math.pow(2, attempt)
            print "[waf_get_ip_set] Retrying in %d seconds..." % (delay)
            time.sleep(delay)
        else:
            break
    else:
        print "[waf_get_ip_set] Failed ALL attempts to call API"

    return response


def waf_update_ip_set(ip_set_id, updates_list):
    response = None

    if updates_list != []:
        waf = boto3.client('waf-regional')
        for attempt in range(API_CALL_NUM_RETRIES):
            try:
                response = waf.update_ip_set(IPSetId=ip_set_id,
                    ChangeToken=waf.get_change_token()['ChangeToken'],
                    Updates=updates_list)
                notify_slack(ip_set_id, updates_list)
            except Exception, e:
                delay = math.pow(2, attempt)
                print "[waf_update_ip_set] Retrying in %d seconds..." % (delay)
                time.sleep(delay)
            else:
                break
        else:
            print "[waf_update_ip_set] Failed ALL attempts to call API"

    return response


def get_ip_set_already_blocked():
    print "[get_ip_set_already_blocked] Start"
    ip_set_already_blocked = []
    try:
        if IP_SET_ID_MANUAL_BLOCK != None:
            response = waf_get_ip_set(IP_SET_ID_MANUAL_BLOCK)
            if response != None:
                for k in response['IPSet']['IPSetDescriptors']:
                    ip_set_already_blocked.append(k['Value'])
    except Exception, e:
        print "[get_ip_set_already_blocked] Error getting WAF IP set"
        print e

    print "[get_ip_set_already_blocked] End"
    return ip_set_already_blocked


def is_already_blocked(ip, ip_set):
    result = False

    try:
        for net in ip_set:
            ipaddr = int(''.join([ '%02x' % int(x) for x in ip.split('.') ]), 16)
            netstr, bits = net.split('/')
            netaddr = int(''.join([ '%02x' % int(x) for x in netstr.split('.') ]), 16)
            mask = (0xffffffff << (32 - int(bits))) & 0xffffffff

            if (ipaddr & mask) == (netaddr & mask):
                result = True
                break
    except Exception, e:
        pass

    return result


def update_waf_ip_set(outstanding_requesters, ip_set_id, ip_set_already_blocked):
    print "[update_waf_ip_set] Start"

    counter = 0
    try:
        if ip_set_id == None:
            print "[update_waf_ip_set] Ignore process when ip_set_id is None"
            return

        updates_list = []
        waf = boto3.client('waf-regional')

        #--------------------------------------------------------------------------------------------------------------
        print "[update_waf_ip_set] \tTruncate [if necessary] list to respect WAF limit"
        #--------------------------------------------------------------------------------------------------------------
        top_outstanding_requesters = {}
        for key, value in sorted(outstanding_requesters.items(), key=lambda kv: kv[1]['max_req_per_min'], reverse=True):
            if counter < LIMIT_IP_ADDRESS_RANGES_PER_IP_MATCH_CONDITION:
                if not is_already_blocked(key, ip_set_already_blocked):
                    top_outstanding_requesters[key] = value
                    counter += 1
            else:
                break

        #--------------------------------------------------------------------------------------------------------------
        print "[update_waf_ip_set] \tRemove IPs that are not in current outstanding requesters list"
        #--------------------------------------------------------------------------------------------------------------
        response = waf_get_ip_set(ip_set_id)
        if response != None:
            for k in response['IPSet']['IPSetDescriptors']:
                ip_value = k['Value'].split('/')[0]
                if ip_value not in top_outstanding_requesters.keys():
                    updates_list.append({
                        'Action': 'DELETE',
                        'IPSetDescriptor': {
                            'Type': 'IPV4',
                            'Value': k['Value']
                        }
                    })
                else:
                    # Dont block an already blocked IP
                    top_outstanding_requesters.pop(ip_value, None)

        #--------------------------------------------------------------------------------------------------------------
        print "[update_waf_ip_set] \tBlock remaining outstanding requesters"
        #--------------------------------------------------------------------------------------------------------------
        for k in top_outstanding_requesters.keys():
            updates_list.append({
                'Action': 'INSERT',
                'IPSetDescriptor': {
                    'Type': 'IPV4',
                    'Value': "%s/32"%k
                }
            })

        #--------------------------------------------------------------------------------------------------------------
        print "[update_waf_ip_set] \tCommit changes in WAF IP set"
        #--------------------------------------------------------------------------------------------------------------
        response = waf_update_ip_set(ip_set_id, updates_list)

    except Exception, e:
        print "[update_waf_ip_set] Error to update waf ip set"
        print e

    print "[update_waf_ip_set] End"
    return counter


def slacking(attachments):
    slack = Slacker(os.environ['SLACK_TOKEN'])
    slack.chat.post_message(
        channel=os.environ['SLACK_CHANNEL'],
        text='',
        as_user=True,
        attachments=attachments
    )


def notify_slack(ip_set_id, updates_list):
    if ip_set_id == IP_SET_ID_AUTO_BLOCK:
        message_added = ':no_pedestrians:IP(s) blocked for next %s minutes' % str(BLACKLIST_BLOCK_PERIOD)
        message_deleted = ':white_check_mark:IP(s) unblocked'
        # else:
        #     message_added = ':pill:IP(s) moved to quarantine for next %s minutes' % str(BLACKLIST_COUNT_PERIOD)
        #     message_deleted = ':white_check_mark:IP(s) removed from quarantine'
        ips_added = []
        ips_deleted = []
        for item in updates_list:
            if item['Action'] == 'INSERT':
                ips_added.append(str(item['IPSetDescriptor']['Value']))
            else:
                ips_deleted.append(str(item['IPSetDescriptor']['Value']))
        if ips_added:
            slacking([{"pretext": message_added, "text": ",\n".join(ips_added)}])
        if ips_deleted:
            slacking([{"pretext": message_deleted, "text": ",\n".join(ips_deleted)}])


def main(stack_name):
    print '[main] Start'
    key_name = 'current_es_outstanding_requesters'

    try:
        #--------------------------------------------------------------------------------------------------------------
        print "[main] \tReading (if necessary) CloudFormation output values"
        #--------------------------------------------------------------------------------------------------------------
        global OUTPUT_BUCKET
        global IP_SET_ID_MANUAL_BLOCK
        global IP_SET_ID_AUTO_BLOCK
        global IP_SET_ID_AUTO_COUNT
        global BLACKLIST_BLOCK_PERIOD
        global BLACKLIST_COUNT_PERIOD
        global REQUEST_PER_MINUTE_LIMIT
        global ES_HOST
        global ES_PORT

        if (OUTPUT_BUCKET == None or IP_SET_ID_MANUAL_BLOCK == None or
            IP_SET_ID_AUTO_BLOCK == None or IP_SET_ID_AUTO_COUNT == None or
            BLACKLIST_BLOCK_PERIOD == None or BLACKLIST_COUNT_PERIOD == None or
            REQUEST_PER_MINUTE_LIMIT == None or
            ES_HOST == None or ES_PORT == None):

            outputs = {}
            cf = boto3.client('cloudformation')
            response = cf.describe_stacks(StackName=stack_name)
            for e in response['Stacks'][0]['Outputs']:
                outputs[e['OutputKey']] = e['OutputValue']

            if OUTPUT_BUCKET == None:
                if 'S3Bucket' in outputs.keys():
                    OUTPUT_BUCKET = outputs['S3Bucket']
            if IP_SET_ID_MANUAL_BLOCK == None:
                IP_SET_ID_MANUAL_BLOCK = outputs['ManualBlockIPSetID']
            if IP_SET_ID_AUTO_BLOCK == None:
                IP_SET_ID_AUTO_BLOCK = outputs['AutoBlockIPSetID']
            if IP_SET_ID_AUTO_COUNT == None:
                IP_SET_ID_AUTO_COUNT = outputs['AutoCountIPSetID']
            if BLACKLIST_BLOCK_PERIOD == None:
                BLACKLIST_BLOCK_PERIOD = int(outputs['WAFBlockPeriod']) # in minutes
            if BLACKLIST_COUNT_PERIOD == None:
                BLACKLIST_COUNT_PERIOD = int(outputs['WAFQuarantinePeriod']) # in minutes
            if REQUEST_PER_MINUTE_LIMIT == None:
                REQUEST_PER_MINUTE_LIMIT = int(outputs['RequestThreshold'])
            if ES_HOST == None:
                ES_HOST = outputs['EsHost']
            if ES_PORT == None:
                ES_PORT = int(outputs['EsPort'])

        print "[main] \t\tOUTPUT_BUCKET = %s"%OUTPUT_BUCKET
        print "[main] \t\tIP_SET_ID_MANUAL_BLOCK = %s"%IP_SET_ID_MANUAL_BLOCK
        print "[main] \t\tIP_SET_ID_AUTO_BLOCK = %s"%IP_SET_ID_AUTO_BLOCK
        print "[main] \t\tIP_SET_ID_AUTO_COUNT = %s"%IP_SET_ID_AUTO_COUNT
        print "[main] \t\tBLACKLIST_BLOCK_PERIOD = %d"%BLACKLIST_BLOCK_PERIOD
        print "[main] \t\tBLACKLIST_COUNT_PERIOD = %d"%BLACKLIST_COUNT_PERIOD
        print "[main] \t\tREQUEST_PER_MINUTE_LIMIT = %d"%REQUEST_PER_MINUTE_LIMIT
        print "[main] \t\tES_HOST = %s"%ES_HOST
        print "[main] \t\tES_PORT = %d"%ES_PORT

        #--------------------------------------------------------------------------------------------------------------
        print "[main] \tReading input data and get outstanding requesters"
        #--------------------------------------------------------------------------------------------------------------
        outstanding_requesters, num_requests = get_elastic_outstanding_requesters()

        #--------------------------------------------------------------------------------------------------------------
        print "[main] \tMerge with current blocked requesters"
        #--------------------------------------------------------------------------------------------------------------
        outstanding_requesters = merge_current_blocked_requesters(key_name, outstanding_requesters)

        #--------------------------------------------------------------------------------------------------------------
        print "[main] \tUpdate new blocked requesters list to S3"
        #--------------------------------------------------------------------------------------------------------------
        write_output(key_name, outstanding_requesters)

        #--------------------------------------------------------------------------------------------------------------
        print "[main] \tUpdate WAF IP Set"
        #--------------------------------------------------------------------------------------------------------------
        ip_set_already_blocked = get_ip_set_already_blocked()
        num_blocked = update_waf_ip_set(outstanding_requesters['block'], IP_SET_ID_AUTO_BLOCK, ip_set_already_blocked)
        num_quarantined = update_waf_ip_set(outstanding_requesters['count'], IP_SET_ID_AUTO_COUNT, ip_set_already_blocked)

        cw = boto3.client('cloudwatch')
        response = cw.put_metric_data(
            Namespace='WAFRateBlacklist-%s' % OUTPUT_BUCKET,
            MetricData=[
                {
                    'MetricName': 'IPBlocked',
                    'Timestamp': datetime.datetime.now(),
                    'Value': num_blocked,
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'IPQuarantined',
                    'Timestamp': datetime.datetime.now(),
                    'Value': num_quarantined,
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'NumRequests',
                    'Timestamp': datetime.datetime.now(),
                    'Value': num_requests,
                    'Unit': 'Count'
                }
            ]
        )
        print '[main] End'
        return outstanding_requesters
    except Exception as e:
        raise e

main('waf-rate')
