#!/usr/bin/env python3

# (c) 2018 Arnim Eijkhoudt <arnime _squigglything_ kpn-cert.nl>
# and Sebastiaan Groot <sebastiaang _monkeytail_ kpn-cert.nl> (for his
# EIQ lib)

# This software is GPLv3 licensed, except where otherwise indicated

import sys
import json
import xml
import re
import optparse
import requests
import urllib3
import time
import smtplib
import string
import eiqcalls
import eiqjson
import pprint
import unicodedata
import pickle
import hashlib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from config import settings


def transform(feedJSON, feedID, options):
    '''
    Take the EIQ JSON objects, extract all observables into lists,
    and transform those into the selected ruletypes.
    '''
    if options.verbose:
        print("U) Converting EIQ JSON objects into a rules group ...")
    entities = []
    for entity in feedJSON:
        if 'extracts' in entity:
            if 'description' in entity['data']:
                description = entity['data']['description']
            else:
                description = ''
            if 'meta' in entity:
                meta = entity['meta']
                tlp = 'AMBER'
                if 'tlp_color' in meta:
                    tlp = meta['tlp_color']
                if 'title' in meta:
                    title = meta['title']
                if entity['extracts']:
                    entry = {title: {
                        'actor-id': [],
                        'description': [description],
                        'domain': [],
                        'email': [],
                        'email-subject': [],
                        'file': [],
                        'ipv4': [],
                        'ipv6': [],
                        'hash-md5': [],
                        'hash-sha1': [],
                        'hash-sha256': [],
                        'hash-sha512': [],
                        'snort': [],
                        'tlp': [tlp],
                        'uri': [],
                        'yara': []
                    }}
                    for extract in entity['extracts']:
                        classification = ''
                        if 'kind' and 'value' in extract:
                            kind, value = extract['kind'], extract['value']
                        if 'meta' in extract:
                            meta = extract['meta']
                            if 'classification' in meta:
                                classification = meta['classification']
                        if classification == 'bad' or \
                           kind == 'snort' or \
                           kind == 'yara':
                            if kind in entry[title]:
                                entry[title][kind].append(value)
                    entities.append(entry)
    return entities


def cleanup(text=None):
    if text:
        text = text.replace('text: ', ' | ')
        text = re.sub(r'<[^>]*?>', '', text)
    return text


def rulegen(entities, options):
    ruleset = []
    if options.type == 's':
        if options.verbose:
            print("U) Building Snort/SourceFire rules ...")
        if not options.rev:
            rev = time.strftime('%Y%m%d00')
        else:
            rev = int(options.rev)
        sid = int(options.sid)
        gid = int(options.gid)
        priority = int(options.priority)
        for entity in entities:
            for title in entity:
                actor = entity[title]['actor-id']
                if not actor:
                    actor = ['unknown']
                tlp = entity[title]['tlp']
                description = cleanup(options.name + " | " +
                                      title)
                message = "TLP:" + ''.join(tlp) + \
                          " | Actor: " + ''.join(actor) + \
                          " | " + ''.join(description)
                message = message.replace('"', '')
                message = unicodedata.normalize('NFKD', message)
                message = ''.join(filter(lambda x: x in string.printable,
                                         message))
            for kind in entity[title]:
                for value in entity[title][kind]:
                    if kind == 'ipv4' or kind == 'ipv6':
                        msg = kind.upper() + " detected | " + message
                        ruleset.append('alert ip $HOME_NET any <> ' +
                                       value + ' any ' +
                                       '(msg:"' + msg + '"; ' +
                                       'priority:' + str(priority) + '; ' +
                                       'sid:' + str(sid) + '; ' +
                                       'gid:' + str(gid) + '; ' +
                                       'classtype:' + options.classtype +
                                       '; ' + 'rev:' + str(rev) +
                                       ')')
                        sid += 1
                    if kind == 'file':
                        msg = kind.upper() + " detected | " + message
                        value = ' '.join("{:02x}".format(ord(c))
                                         for c in value)
                        ruleset.append('alert tcp $HOME_NET any -> ' +
                                       options.dest + ' any ' +
                                       '(msg:"' + msg + '"; ' +
                                       'content:"|' + value + '|"; ' +
                                       'priority:' + str(priority) + '; ' +
                                       'sid:' + str(sid) + '; ' +
                                       'gid:' + str(gid) + '; ' +
                                       'classtype:' + options.classtype +
                                       '; ' + 'rev:' + str(rev) +
                                       ')')
                        sid += 1
                    if kind == 'uri':
                        msg = kind.upper() + " detected | " + message
                        uri = urllib3.util.parse_url(value)
                        dest = options.dest
                        host = uri.host
                        port = str(uri.port)
                        # Check if we need to write SourceFire/Snort rules
                        # that need to compensate for proxy traffic
                        if settings.HTTP_PROXYSERVER:
                            http_ports = str(settings.HTTP_PROXYSERVERPORT)
                            dest = settings.HTTP_PROXYSERVER
                        else:
                            if uri.port:
                                http_ports = str(uri.port)
                            else:
                                http_ports = settings.HTTP_PORTS
                        # Remove variables in GET request to prevent
                        # overly long content checks, and strip out the
                        # http[s] part
                        value = re.sub(r'https?:\/\/','',value)
                        if '?' in value:
                            value = value.split('?')[0]
                        # Check if the URI contains UTF/high-ASCII stuff
                        # that might break SourceFire/Snort parsing
                        newvalue = unicodedata.normalize('NFKD', value)
                        newvalue = ''.join(filter(lambda x: x in
                                                  string.printable, newvalue))
                        content = ''
                        if newvalue != value:
                            content += 'content:"|'
                            value = ' '.join("{:02x}".format(ord(c))
                                             for c in value)
                            content += value + '|"; '
                        else:
                            content += 'content:"' + value + '"; '
                            content += 'fast_pattern:only; '
                            content += 'nocase; '
                        ruleset.append('alert tcp $HOME_NET any -> ' +
                                       dest + ' ' +
                                       http_ports + ' ' +
                                       '(msg:"' + msg + '"; ' +
                                       'flow:to_server,established; ' +
                                       content +
                                       'priority:' + str(priority) + '; ' +
                                       'sid:' + str(sid) + '; ' +
                                       'gid:' + str(gid) + '; ' +
                                       'classtype:' + options.classtype +
                                       '; ' + 'rev:' + str(rev) +
                                       ')')
                        sid += 1
                    if kind == 'domain':
                        msg = kind.upper() + " detected | " + message
                        domainparts = value.split('.')
                        content = ''
                        for part in domainparts:
                            content += '|' + hex(len(part))[2:].zfill(2) + \
                                       '|' + part
                        content += '|00|'
                        ruleset.append('alert udp $HOME_NET any -> ' +
                                       options.dest + ' 53 ' +
                                       '(msg:"' + msg + '"; ' +
                                       'byte_test:1,!&,0xF8,2; ' +
                                       'content:"' + content + '"; ' +
                                       'fast_pattern:only; ' +
                                       'priority:' + str(priority) + '; ' +
                                       'sid:' + str(sid) + '; ' +
                                       'gid:' + str(gid) + '; ' +
                                       'classtype:' + options.classtype +
                                       '; ' + 'rev:' + str(rev) +
                                       ')')
                        sid += 1
                        ruleset.append('alert tcp $HOME_NET any -> ' +
                                       options.dest + ' 53 ' +
                                       '(msg:"' + msg + '"; ' +
                                       'byte_test:1,!&,0xF8,2; ' +
                                       'content:"' + content + '"; ' +
                                       'fast_pattern:only; ' +
                                       'priority:' + str(priority) + '; ' +
                                       'sid:' + str(sid) + '; ' +
                                       'gid:' + str(gid) + '; ' +
                                       'classtype:' + options.classtype +
                                       '; ' + 'rev:' + str(rev) +
                                       ')')
                        sid += 1
                    if kind == 'email' or kind == 'email-subject':
                        msg = kind.upper() + " detected | " + message
                        value = ' '.join("{:02x}".format(ord(c))
                                         for c in value)
                        ruleset.append('alert tcp $HOME_NET any <> ' +
                                       options.dest + ' ' +
                                       settings.SMTP_PORTS +
                                       ' (msg:"' + msg + '"; ' +
                                       'content:"|' + value + '|"; ' +
                                       'priority:' + str(priority) + '; ' +
                                       'sid:' + str(sid) + '; ' +
                                       'gid:' + str(gid) + '; ' +
                                       'classtype:' + options.classtype +
                                       '; ' + 'rev:' + str(rev) +
                                       ')')
                        sid += 1
                        ruleset.append('alert tcp $HOME_NET any <> ' +
                                       options.dest + ' ' +
                                       settings.POP3_PORTS +
                                       ' (msg:"' + msg + '"; ' +
                                       'content:"|' + value + '|"; ' +
                                       'priority:' + str(priority) + '; ' +
                                       'sid:' + str(sid) + '; ' +
                                       'gid:' + str(gid) + '; ' +
                                       'classtype:' + options.classtype +
                                       '; ' + 'rev:' + str(rev) +
                                       ')')
                        sid += 1
                        ruleset.append('alert tcp $HOME_NET any <> ' +
                                       options.dest + ' ' +
                                       settings.IMAP_PORTS + ' ' +
                                       '(msg:"' + msg + '"; ' +
                                       'content:"|' + value + '|"; ' +
                                       'priority:' + str(priority) + '; ' +
                                       'sid:' + str(sid) + '; ' +
                                       'gid:' + str(gid) + '; ' +
                                       'classtype:' + options.classtype +
                                       '; ' + 'rev:' + str(rev) +
                                       ')')
                        sid += 1
                    if kind == 'snort':
                        msg = kind.upper() + " detected | " + message
                        ruleset.append(value)
    finalruleset = []
    sidmap = {}
    newsidmap = {}
    newrules = []
    sidfind = re.compile(r'sid:\d+; ')
    revfind = re.compile(r' rev:\d+')
    msgfind = re.compile(r'msg:\"[^\"]+\"; ')
    try:
        with open(settings.SIDFILE, 'rb') as sidfile:
            sidmap = pickle.load(sidfile)
    except IOError:
        pass
    for line in ruleset:
        hashline = sidfind.sub('', line)
        hashline = revfind.sub('', hashline)
        hashline = msgfind.sub('', hashline)
        rulehash = hashlib.sha256()
        rulehash.update(hashline.strip().encode('utf-8'))
        rulehexdigest = rulehash.hexdigest()
        # Check if a rule already exists in the sidmap file
        if rulehexdigest in sidmap:
            existingsid, existingrev = sidmap[rulehexdigest]
            newsidmap[rulehexdigest] = (existingsid, existingrev)
            # Check if the revision number of the existing rule is older
            # than right now. If the existing rule in the sidmap is newer,
            # something must be wrong; don't do anything and drop the
            # rule from the final rule list.
            if existingrev <= rev:
                # The existing rule is older, so reuse the older SID from
                # the sidmap file
                rule = sidfind.sub('sid:' + str(existingsid) + '; ', line)
                finalruleset.append(rule)
        else:
            newrules.append(line)
        # Start counting at the base sid from the options again
        newsid = int(options.sid)
        usedsids = []
        # Now build a list of the SIDs that are in use in the new sidmap
        for rulehexdigest in newsidmap:
            existingsid, existingrev = newsidmap[rulehexdigest]
            usedsids.append(existingsid)
        # Since the remainder of the ruleset is rules that did not
        # previously exist, add them to the final ruleset, while at the
        # same time reusing the stale SIDs
        for line in newrules:
            hashline = sidfind.sub('', line)
            hashline = revfind.sub('', hashline)
            rulehash = hashlib.sha256()
            rulehash.update(hashline.strip().encode('utf-8'))
            rulehexdigest = rulehash.hexdigest()
            while line not in finalruleset:
                # As long as the new rule is not in the final ruleset,
                # try to find a SID for it that is free
                if not newsid in usedsids:
                    rule = sidfind.sub('sid:' + str(newsid) + '; ', line)
                    newsidmap[rulehexdigest] = (newsid, rev)
                    finalruleset.append(line)
                newsid += 1
    if not options.simulate:
        try:
            with open(settings.SIDFILE, 'wb') as sidfile:
                pickle.dump(newsidmap, sidfile, pickle.HIGHEST_PROTOCOL)
        except:
            if options.verbose:
                print("An error occurred writing the sidmap to disk!")
                raise
    if options.verbose:
        print("U) Ruleset is: ")
        print(("\n".join(finalruleset)))
    return finalruleset


def download(feedID, options):
    '''
    Download the given feed number from the EclecticIQ JSON instance
    '''
    if not settings.EIQSSLVERIFY:
        if options.verbose:
            print("W) You have disabled SSL verification for EIQ, " +
                  "this is not recommended.")
    eiqAPI = eiqcalls.EIQApi(insecure=not(settings.EIQSSLVERIFY))
    eiqHost = settings.EIQHOST + settings.EIQVERSION
    eiqFeed = settings.EIQFEEDS + '/' + str(feedID)
    eiqAPI.set_host(eiqHost)
    eiqAPI.set_credentials(settings.EIQUSER, settings.EIQPASS)
    eiqToken = eiqAPI.do_auth()
    eiqHeaders = {}
    eiqHeaders['Authorization'] = 'Bearer %s' % (eiqToken['token'],)
    try:
        if options.verbose:
            print("U) Contacting " + eiqHost + eiqFeed + ' ...')
        response = eiqAPI.do_call(endpt=eiqFeed,
                                  headers=eiqHeaders,
                                  method='GET')
    except:
        print("E) An error occurred contacting the EIQ URL at " +
              feedURL)
        raise
    if not response or ('errors' in response):
        if response:
            for err in response['errors']:
                print('[error %d] %s' % (err['status'], err['title']))
                print('\t%s' % (err['detail'], ))
        else:
            print('unable to get a response from host')
            sys.exit(1)
    if 'content_blocks' not in response['data']:
        if options.verbose:
            print("E) No content blocks in feed ID!")
    else:
        if options.verbose:
            print("U) Attempting to download latest feed content ...")
        content_block = response['data']['content_blocks'][0]
        content_block = content_block.replace(settings.EIQVERSION, "")
        response = eiqAPI.do_call(endpt=content_block,
                                  headers=eiqHeaders,
                                  method='GET')
        if options.verbose:
            pprint.pprint(response)
        if 'entities' not in response:
            print("E) No entities in response!")
        else:
            return response['entities']


def process(ruleset, options):
    ruleset = ('\n'.join(ruleset))+'\n'
    if options.action == 'mail' or options.action == 'm':
        timestamp = time.strftime('%Y-%m-%d, %H:%M:%S')
        filename = time.strftime('%Y%m%d-%H%M%S-') + settings.OUTPUTFILE
        if options.verbose:
            print("U) Sending ruleset to e-mail ... ")
            print("U) From:    " + settings.EMAILFROM)
            print("U) To:      " + settings.EMAILTO)
            print("U) Subject: " + settings.EMAILSUBJECT)
        if not options.simulate:
            msg = MIMEMultipart()
            msg['Subject'] = settings.EMAILSUBJECT + " for " + timestamp
            msg['From'] = settings.EMAILFROM
            msg['To'] = settings.EMAILTO
            msg['Date'] = formatdate()
            msg['Message-Id'] = make_msgid()
            content = "This email contains the output of the eiq_to_ids.py "
            content += "run for " + timestamp + ". The generated ruleset "
            content += "has been included as a text file attachment.\n"
            content += "\n"
            content += "Kind regards,\n"
            content += "\n"
            content += settings.EMAILFROM
            content += " - (this was an automatically generated message)"
            msg.attach(MIMEText(content))
            part = MIMEApplication(ruleset, Name=filename)
            content_disposition = 'attachment; filename="%s"' % filename
            part['Content-Disposition'] = content_disposition
            msg.attach(part)
            smtp = smtplib.SMTP(settings.EMAILSERVER)
            try:
                smtp.send_message(msg)
            except:
                print("E) An error occurred sending e-mail!")
                raise
        else:
            print("U) Not sending e-mail as simulate option is set!")
    if options.action == 'file' or options.action == 'f':
        timestamp = time.strftime('%Y%m%d-%H%M%S-')
        try:
            if not options.simulate:
                if options.verbose:
                    print("U) Writing ruleset to file: " +
                          timestamp + settings.OUTPUTFILE)
                with open(timestamp + settings.OUTPUTFILE, 'w') as file:
                    file.writelines(ruleset)
            else:
                print("U) Not writing anything to disk, as " +
                      "simulation option was set!")
        except:
            print("E) An error occurred writing to disk!")
            raise


if __name__ == "__main__":
    cli = optparse.OptionParser(usage="usage: %prog [-v | -t | -s " +
                                      "| -n | -d | -a] <EIQ feed ID>")
    cli.add_option('-v', '--verbose',
                   dest='verbose',
                   action='store_true',
                   default=False,
                   help='[optional] Enable progress/error info (default: ' +
                        'disabled)')
    cli.add_option('-t', '--type',
                   dest='type',
                   default='s',
                   help='[optional] Set the type of IDS / SIEM rule you ' +
                        'wish to create: [s]ourcefire/Snort (default). ')
    cli.add_option('-a', '--action',
                   dest='action',
                   default=settings.ACTION,
                   help='Specify the action to take with the generated ' +
                        'ruleset: [f]ile, [m]ail (default: write to ' +
                        'the [f]ilename specified in settings.py)')
    cli.add_option('-d', '--dest',
                   dest='dest',
                   default='any',
                   help='[optional] Set the destination network name you ' +
                        'want to create the ruleset for (default: \'any\')')
    cli.add_option('-s', '--simulate',
                   dest='simulate',
                   action='store_true',
                   default=False,
                   help='[optional] Do not actually generate anything, ' +
                        'just simulate everything. Mostly useful with ' +
                        'the -v/--verbose flag for debugging purposes.')
    cli.add_option('-i', '--sid',
                   dest='sid',
                   default=settings.SID,
                   help='[optional] Override the sid to start counting ' +
                        'from (default: ' + str(settings.SID) + ')')
    cli.add_option('-g', '--gid',
                   dest='gid',
                   default=settings.GID,
                   help='[optional] Override the gid from the configuration ' +
                        'file (default: ' + str(settings.GID) + ')')
    cli.add_option('-p', '--priority',
                   dest='priority',
                   default=settings.PRIORITY,
                   help='[optional] Override the default priority from the ' +
                        'configuration file (default: ' +
                        str(settings.PRIORITY) + ')')
    cli.add_option('-c', '--classtype',
                   dest='classtype',
                   default=settings.CLASSTYPE,
                   help='[optional] Override the classtype setting from ' +
                        'the configuration file (default: ' +
                        settings.CLASSTYPE + ')')
    cli.add_option('-r', '--rev',
                   dest='rev',
                   default=None,
                   help='[optional] Specify the rev id to start counting ' +
                        'from. Particularly important when creating Snort ' +
                        'SourceFire rulesets, as not all instances support ' +
                        'easy deletion/disabling. Using the rev counter ' +
                        'ensures that the rules can be loaded correctly ' +
                        '(default: use date/time: YYYYMMDD00)')
    cli.add_option('-n', '--name',
                   dest='name',
                   default=settings.COMMENT,
                   help='[optional] Override the default comment from ' +
                        'the configuration file (default: COMMENT field ' +
                        'from the settings.py file)')
    cli.add_option('-m', '--maliciousness',
                   dest='maliciousness',
                   default=1,
                   help='[optional] Set minimum maliciousness')
    (options, args) = cli.parse_args()
    if len(args) < 1:
        cli.print_help()
        sys.exit(1)
    if len(args) > 1:
        print("E) Please specify exactly a feedID from EclecticIQ.")
        sys.exit(1)
    else:
        try:
            feedID = int(args[0])
        except:
            print("E) Please specify a numeric feedID only.")
            raise
        feedDict = download(feedID, options)
        entities = transform(feedDict, feedID, options)
        ruleset = rulegen(entities, options)
        if ruleset:
            process(ruleset, options)
