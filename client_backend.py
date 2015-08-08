import threading
import Queue
import time
from datetime import datetime, timedelta
#import paho.mqtt.client as mqtt
#from paho.mqtt.client import MQTT_ERR_SUCCESS, mqtt_cs_connected
from mqtt_client import MQTT_ERR_SUCCESS, mqtt_cs_connected, MQTTMessage
from mqtt_client import Client as mqtt
from storage import Storage
import gnupg
from messaging import Message, Messaging, Contact
from utilities import queue_task, current_time, got_pgpkey,parse_user_name, full_name, Listing, get_age
import json
import socks
import socket
from calendar import timegm
from time import gmtime
import random
import re
from collections import defaultdict
from pprint import pprint
import string
from platform import system as get_os
import textwrap
from constants import *
import pybitcointools as btc
from btc_utils import *

class messaging_loop(threading.Thread):
# Authentication shall set the password to a PGP clear-signed message containing the follow data only
# broker-hostname:pgpkeyid:UTCdatetime
# The date time shall not contain seconds and will be chekced on the server to be no more than +/- 5 minutes fromcurrent server time

    def __init__ (self, pgpkeyid, pgppassphrase, dbpassphrase, database, homedir, appdir, q, q_res, q_data, workoffline=False):
        self.targetbroker = None
        self.mypgpkeyid = pgpkeyid
        self.q = q # queue for client (incoming queue_task requests)
        self.q_res = q_res  # results queue for client (outgoing)
        self.q_data = q_data  # results queue for client (outgoing)
        self.database = database
        self.homedir = homedir
        if get_os() == 'Windows':
            self.pgpdir = homedir + '/application data/gnupg'
        else:
            self.pgpdir = homedir + '/.gnupg'
        self.appdir = appdir
        self.test_mode = False
        self.dbsecretkey = dbpassphrase
        self.onion_brokers = []
        self.i2p_brokers = []
        self.clearnet_brokers = []
        self.gpg = gnupg.GPG(gnupghome=self.pgpdir,options={'--primary-keyring="' + self.appdir + '/pubkeys.gpg"'})
        self.pgp_passphrase = pgppassphrase
        self.profile_text = ''
        self.display_name = None
        self.myMessaging = Messaging(self.mypgpkeyid,self.pgp_passphrase,self.pgpdir,appdir)
        self.sub_inbox = str("user/" + self.mypgpkeyid + "/inbox")
        self.pub_profile = str("user/" + self.mypgpkeyid + "/profile")
        self.pub_key = str("user/" + self.mypgpkeyid + "/key")
        self.pub_items = str("user/" + self.mypgpkeyid + "/items")
        self.pub_directory = str("user/" + self.mypgpkeyid + "/directory")
        self.storageDB = Storage
        self.connected = False
        self.workoffline = workoffline
        self.shutdown = False
        self.message_retention = 30 # default 30 day retention of messages before auto-purge
        self.allow_unsigned = True # allow unsigned PMs by default
        self.task_state_messages=defaultdict(defaultdict)  # This will hold the state of any outstanding message tasks
        self.task_state_pgpkeys=defaultdict(defaultdict)  # This will hold the state of any outstanding key retrieval tasks
        self.memcache_listing_items=defaultdict(defaultdict)  # This will hold cached listing items, entries will be created as needed
        self.publish_identity = True
        self.btc_master_key = ''
        threading.Thread.__init__ (self)

    def on_connect(self, client, userdata, flags, rc):  # TODO: Check that these parameters are right, had to add "flags" which may break "rc"
        print "Connected"
        self.connected = True
        flash_msg = queue_task(0,'flash_status','On-line')
        self.q_res.put(flash_msg)
        flash_msg = queue_task(0,'flash_message','Connected to broker ' + self.targetbroker)
        self.q_res.put(flash_msg)
        self.setup_message_queues(client)
        # TODO: Call function to process any queued outbound messages(pm & transaction) - github issue #5

    def on_disconnect(self, client, userdata, rc):
        self.connected = False
        flash_msg = queue_task(0,'flash_status','Off-line')
        self.q_res.put(flash_msg)
        if self.shutdown:
            flash_msg = queue_task(0,'flash_message','Disconnected from ' + self.targetbroker)
        else:
            flash_msg = queue_task(0,'flash_message','Broker was disconnected, attempting to reconnect to ' + self.targetbroker)
            try:
                client.reconnect()
            except:
                print "Reconnection failure"
        self.q_res.put(flash_msg)

    # TODO - make use of onpublish and onsubscribe callbacks instead of checking status of call to publish or subscribe
    def on_publish(self, client, userdata, mid):
        print "On publish for " + str(mid) + " " + str(userdata)

    def on_subscribe(self, client, userdata, mid, granted_qos):
        print "On subscribe for " + str(mid) + " " + str(userdata)

    def on_message(self, client, userdata, msg):
        # Key blocks and directory entries are a special case because they are not signed so we check for them first
        if re.match('user\/[A-F0-9]{16}\/key',msg.topic):
            # Here is a key, store it and unsubscribe
            print "Key Retrieved from " + str(msg.topic)
            client.unsubscribe(msg.topic)
            #TODO: Check we have really been sent a PGP key block and check if it really is the same as the topic key
            keyid = msg.topic[msg.topic.index('/')+1:msg.topic.rindex('/')]
            try:
                state=self.task_state_pgpkeys[keyid]['state']
            except KeyError:
                state=None
            if state == KEY_LOOKUP_STATE_REQUESTED:
                session = self.storageDB.DBSession()
                cachedkey = self.storageDB.cachePGPKeys(key_id=keyid,
                                                        updated=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
                                                        keyblock=msg.payload )
                session.add(cachedkey)
                session.commit()
                self.task_state_pgpkeys[keyid]['state'] = KEY_LOOKUP_STATE_FOUND
                print "Retrieved key committed"
            else:
                print "Dropping unexpected pgp key received for keyid: " + keyid
                #TODO: Shall we just always allow keys to be received?
            return None
#            imp_res = self.gpg.import_keys(msg.payload) # we could import it here but we use the local keyserver
        elif re.match('user\/[A-F0-9]{16}\/directory',msg.topic):
            # Here is a directory entry, store it and unsubscribe
            client.unsubscribe(msg.topic)
            keyid = msg.topic[msg.topic.index('/')+1:msg.topic.rindex('/')]
            print "Directory entry: " + msg.payload + " " + msg.topic
            #TODO: Use a memcache rather than write direct to database, use a periodic task to save memcache to cacheDirectory table (every 30 seconds or so)
            #TODO: This memory cache will be necessary to deal with a large number of users in the directory
            print "Adding directory entry "
            display_dict=json.loads(msg.payload)
            session = self.storageDB.DBSession()
            if session.query(self.storageDB.cacheDirectory).filter(self.storageDB.cacheDirectory.key_id == keyid).count() > 0: # If it already exists then update
                direntry = session.query(self.storageDB.cacheDirectory).filter(self.storageDB.cacheDirectory.key_id == keyid).update({
                                                                self.storageDB.cacheDirectory.updated:datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
                                                                self.storageDB.cacheDirectory.display_name:display_dict['display_name']
                                                            })
            else:
                direntry = self.storageDB.cacheDirectory(key_id=keyid,
                                                            updated=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
                                                            display_name=display_dict['display_name'])

                session.add(direntry)
            session.commit()
            return
        #  let us see if we can parse the base message and have the necessary public key to check signature, if any
        incoming_message = self.myMessaging.GetMessage(msg.payload,self,msg.topic,allow_unsigned=self.allow_unsigned)
        # If this message was deferred lets stack it...
        if incoming_message == False:
            print "Message received but not processed correctly so dropped..."
            return
        elif incoming_message == MSG_STATE_KEY_REQUESTED:
            # getmessage should have already updated task_state_pgpkeys and task_state_messages
            print "Incoming message has been deferred while signing key is requested"
            return

        if msg.topic == self.sub_inbox and (self.allow_unsigned or incoming_message.signed ):
            message = incoming_message
            if message == False:  print "Message was invalid"
            elif message.type == 'Private Message':
                # Calculate purge date for this message
                purgedate = datetime.now()+timedelta(days=self.message_retention)
                flash_msg = queue_task(0,'flash_message','Private message received from ' + message.sender)
                self.q_res.put(flash_msg)
                session = self.storageDB.DBSession()
                new_db_message = self.storageDB.PrivateMessaging(
                                                                    sender_key=message.sender,
                                                                    recipient_key=message.recipient,
                                                                    message_id=message.id,
                                                                    message_purge_date=purgedate,
                                                                    message_date=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
                                                                    subject=message.subject,
                                                                    body=message.body,
                                                                    message_sent=False,
                                                                    message_read=False,
                                                                    message_direction="In"
                                                                 )
                session.add(new_db_message)
                session.commit()
            elif message.type == 'Transaction':
                flash_msg = queue_task(0,'flash_message','Transaction message received from ' + message.sender)
                self.q_res.put(flash_msg)
#                session = self.storageDB.DBSession()
#                new_db_message = self.storageDB.PrivateMessaging(
#                                                                    sender_key=message.sender,
#                                                                    recipient_key=message.recipient,
#                                                                    message_id=message.id,
#                                                                    message_purge_date=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
#                                                                    message_date=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
#                                                                    subject=message.subject,
#                                                                    body=message.body,
#                                                                    message_sent=False,
#                                                                    message_read=False,
#                                                                    message_direction="In"
#                                                                 )
#                session.add(new_db_message)
#                session.commit()
        elif re.match('user\/[A-F0-9]{16}\/profile',msg.topic) and incoming_message.signed:
            # Here is a profile, store it and unsubscribe
            client.unsubscribe(msg.topic)
            keyid = msg.topic[msg.topic.index('/')+1:msg.topic.rindex('/')]
            #TODO: Check we have really been sent a valid profile message for the key indicated
            #print msg.payload
            profile_message = incoming_message # self.myMessaging.GetMessage(msg.payload,self,allow_unsigned=False) # Never allow unsigned profiles
            if profile_message:
                if not keyid==profile_message.sender:
                    print "Profile was signed by a different key - discarding..."
                else:
                    profile_text = profile_message.sub_messages['profile']
                    session = self.storageDB.DBSession()

                    if 'display_name' in profile_message.sub_messages and 'profile' in profile_message.sub_messages and 'avatar_image' in profile_message.sub_messages:
                        if session.query(self.storageDB.cacheProfiles).filter(self.storageDB.cacheProfiles.key_id == keyid).count() > 0: # If it already exists then update
                            cachedprofile = session.query(self.storageDB.cacheProfiles).filter(self.storageDB.cacheProfiles.key_id == keyid).update({
                                                                            self.storageDB.cacheProfiles.updated:datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
                                                                            self.storageDB.cacheProfiles.display_name:profile_message.sub_messages['display_name'],
                                                                            self.storageDB.cacheProfiles.profile_text:profile_message.sub_messages['profile'],
                                                                            self.storageDB.cacheProfiles.avatar_base64:profile_message.sub_messages['avatar_image']
                                                                        })
                        else:
                            cachedprofile = self.storageDB.cacheProfiles(key_id=keyid,
                                                updated=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
                                                display_name=profile_message.sub_messages['display_name'],
                                                profile_text=profile_message.sub_messages['profile'],
                                                avatar_base64=profile_message.sub_messages['avatar_image'])
                            session.add(cachedprofile)
                        session.commit()






                        session.commit()
                    else:
                        print "Profile message did not contain mandatory fields"
        #            imp_res = self.gpg.import_keys(msg.payload) # we could import it here but we use the local keyserver
            else:
                print "No profile message found in profile returned from " + keyid
        elif re.match('user\/[A-F0-9]{16}\/items',msg.topic) and incoming_message.signed:
            # Here is a listings message, store it and unsubscribe
            client.unsubscribe(msg.topic)
            keyid = msg.topic[msg.topic.index('/')+1:msg.topic.rindex('/')]
            #print msg.payload
            listings_message = incoming_message # self.myMessaging.GetMessage(msg.payload,self,allow_unsigned=False) # Never allow unsigned listings
            if listings_message:
#                print listings_message
                if not keyid==listings_message.sender:
                    print "Listings were signed by a different key - discarding..."
                else:
                    listings = json.dumps(listings_message.sub_messages)
                    print "Listings message received from " + keyid
                    # add this to the cachelistings table
                    if listings_message.type == 'Listings Message' and listings:
                        session = self.storageDB.DBSession()
                        if session.query(self.storageDB.cacheListings).filter(self.storageDB.cacheListings.key_id == keyid).count() > 0: # If it already exists then update
                            print "There appears to be an existing listings message in the db cache for user " + keyid
                            cachedlistings = session.query(self.storageDB.cacheListings).filter(self.storageDB.cacheListings.key_id == keyid).update({
                                                                            self.storageDB.cacheListings.updated:datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
                                                                            self.storageDB.cacheListings.listings_block:listings
                                                                        })
                        else:
                            print "There is nothing in the listings cache for user, creating new cache entry"
                            cachedlistings= self.storageDB.cacheListings(key_id=keyid,
                                                                updated=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
                                                                listings_block=listings)
                            session.add(cachedlistings)
                        session.commit()
                    if not listings_message.sub_messages:
                        self.q_data.put ('none') # user has no listings, return 'none'
                    else:
                        print "Extracting items from valid listings message..."
                        verified_listings = self.get_items_from_listings(keyid,listings_message.sub_messages)
                        self.q_data.put (verified_listings)
            else:
                print "No items message found in listings returned from " + keyid
        else:
            if incoming_message.signed:
                flash_msg = queue_task(0,'flash_error','Message received for non-inbox topic - ' + msg.topic)
                print "Unsigned message recv: " + msg.payload
            else:
                flash_msg = queue_task(0,'flash_error','Unsigned message received for topic - ' + msg.topic)
                print "Non PM message recv: " + msg.payload
            self.q_res.put(flash_msg)


    def setup_message_queues(self,client):
        # Sub to directory each conenction for now
        # TODO ONLY PUB IF WE NEED TO, check our topics with subs first and only publish if there is a difference with the local db copy
        client.subscribe('user/+/directory',1)  # TODO: We definitely don't want to do this each time user connects - put such SUBs in an one time client SUB setup
        client.subscribe(self.sub_inbox,1)                      # Our generic incoming queue, qos=1
        client.publish(self.pub_key,self.gpg.export_keys(self.mypgpkeyid,False,minimal=True),  1,True)      # Our published pgp key block, qos=1, durable
        # Calculate stealth address from the first child key of the master key
        stealth_address = create_stealth_address(btc.privkey_to_pubkey(btc.bip32_extract_key(btc.bip32_ckd(self.btc_master_key,1))))
        # build and send profile message
        profile_message = Message()
        profile_message.type = 'Profile Message'
        profile_message.sender = self.mypgpkeyid
        profile_dict = {}
        profile_dict['display_name'] = self.display_name
        profile_dict['profile'] = self.profile_text
        profile_dict['avatar_image'] = self.avatar_image
        profile_dict['stealth_address'] = stealth_address
        profile_message.sub_messages = profile_dict # todo: use .append to add a submessage instead
        profile_out_message = Messaging.PrepareMessage(self.myMessaging,profile_message)
        client.publish(self.pub_profile,profile_out_message,1,True)      # Our published profile queue, qos=1, durable
        # build and send listings message
        listings_out_message = Messaging.PrepareMessage(self.myMessaging,self.create_listings_msg())
        client.publish(self.pub_items,listings_out_message,1,True)      # Our published items queue, qos=1, durable
        # build and send directory entry if user selected to publish
        if self.publish_identity:
#           directory_message = Message()
#            directory_message.type = 'Directory Message'
#            directory_message.sender = self.mypgpkeyid
            directory_dict = {}
            directory_dict['display_name'] = self.display_name
            str_directory = json.dumps(directory_dict)
            print "Directory string to publish : " + str_directory
            client.publish(self.pub_directory,str_directory,1,True)      # Our published items queue, qos=1, durable

    def create_listings_msg(self):
        listings_message = Message()
        listings_message.type = 'Listings Message'
        listings_message.sender = self.mypgpkeyid
        # Calculate stealth address from the first child key of the master key
        stealth_address = create_stealth_address(btc.privkey_to_pubkey(btc.bip32_extract_key(btc.bip32_ckd(self.btc_master_key,1))))
        session = self.storageDB.DBSession()
        listings = session.query(self.storageDB.Listings).filter_by(public=True).all()  # We're only going to publish listings marked as public=True  # .filter_by(public=True)
        if not listings:
            listings_message.body = "User is not publishing any items"
            listings_message.sub_messages = None
        else: # We have listings, iterate and add a sub-message per listing

            for listing_item in listings:
                listings_dict = {}
                listings_dict['id'] = listing_item.id
                listings_dict['item'] = listing_item.title
                listings_dict['category'] = listing_item.category
                listings_dict['description'] = listing_item.description
                listings_dict['image'] = listing_item.image_base64
                listings_dict['unit_price'] = listing_item.price
                listings_dict['currency'] = listing_item.currency_code
                listings_dict['qty'] = listing_item.qty_available
                listings_dict['max_order_qty'] = listing_item.order_max_qty
                listings_dict['publish_date'] = current_time()
                listings_dict['stealth_address'] = stealth_address
                listings_dict['shipping_options'] = listing_item.shipping_options
                # listings_dict_str = json.dumps(listings_dict)
                listings_dict_str = json.dumps(listings_dict,sort_keys=True)
                listings_dict_str = textwrap.fill(listings_dict_str, 80, drop_whitespace=False)
                signed_item = str(self.gpg.sign(listings_dict_str,keyid=self.mypgpkeyid,passphrase=self.pgp_passphrase))
                listings_message.sub_messages.append(signed_item)#listings_dict_str)
                #listings_dict.clear()
        return listings_message

    def sendMessage(self,client,message):
        message.recipient = parse_user_name(message.recipient).pgpkey_id
        # do we have this key?
        if not got_pgpkey(self.storageDB, message.recipient):
            # need to get the recipients public key
            self.task_state_messages[message.id]['state']= MSG_STATE_NEEDS_KEY
            self.task_state_messages[message.id]['message']= message
#            return False # Message processing is deferred pending a key, added message to task_state_messages todo remove line to saveto db even if no key is present
        else:
            outMessage = self.myMessaging.PrepareMessage(message)
            if not outMessage:
                flash_msg = queue_task(0,'flash_error','Error: Message could not be prepared')
                self.q_res.put(flash_msg)
                return False
            res = client.publish("user/" + message.recipient + "/inbox", outMessage, 1, False)
            if res[0] == MQTT_ERR_SUCCESS:
                flash_msg = queue_task(0,'flash_message','Message sent to ' + message.recipient)
                message.sent = True
                self.q_res.put(flash_msg)
            else:
                flash_msg = queue_task(0,'flash_message','Message queued for ' + message.recipient)
                self.q_res.put(flash_msg)
                self.task_state_messages[message.id]['state']= MSG_STATE_QUEUED
                self.task_state_messages[message.id]['message']= message
        # Calculate purge date for this message
        purgedate = datetime.now()+timedelta(days=self.message_retention)
        if message.datetime_sent:
            formatted_msg_sent_date = datetime.strptime(message.datetime_sent,"%Y-%m-%d %H:%M:%S")
        else:
            formatted_msg_sent_date = None
        # Write the message to the database as long as we havent already - check to see if message id already exists first
        session = self.storageDB.DBSession()
        if message.type == "Txn Message": # This is a transaction message
            new_db_message = self.storageDB.PrivateMessaging(
                                                                sender_key=message.sender,
                                                                sender_name=message.sender_name,
                                                                recipient_key=message.recipient,
                                                                recipient_name=message.recipient_name,
                                                                message_id=message.id,
                                                                message_purge_date = purgedate, # this will not be used directly (no purge for txn msgs)
                                                                message_date=formatted_msg_sent_date,
                                                                subject=message.subject,
                                                                body=message.body,
                                                                message_sent=message.sent,
                                                                message_read=message.read,
                                                                message_direction="Out"
                                                             )
            session.add(new_db_message)

        elif message.type == "Private Message":
            if session.query(self.storageDB.PrivateMessaging).filter(self.storageDB.PrivateMessaging.message_id == message.id).count() > 0: # If it already exists then update
                # Message ID already esists in db. in theory all we could be doing now is updating sent status and sent date
                message_to_update = session.query(self.storageDB.PrivateMessaging).filter(self.storageDB.PrivateMessaging.message_id == message.id).update({
                                                                self.storageDB.PrivateMessaging.message_date:formatted_msg_sent_date,
                                                                self.storageDB.PrivateMessaging.message_sent:message.sent
                                                            })
            else:
                new_db_message = self.storageDB.PrivateMessaging(
                                                                    sender_key=message.sender,
                                                                    sender_name=message.sender_name,
                                                                    recipient_key=message.recipient,
                                                                    recipient_name=message.recipient_name,
                                                                    message_id=message.id,
                                                                    message_purge_date = purgedate,
                                                                    message_date=formatted_msg_sent_date,
                                                                    subject=message.subject,
                                                                    body=message.body,
                                                                    message_sent=message.sent,
                                                                    message_read=message.read,
                                                                    message_direction="Out"
                                                                )
                session.add(new_db_message)
        session.commit()

    def delete_pm(self,id):
        flash_msg = queue_task(0,'flash_message','Deleted private message ')
        self.q_res.put(flash_msg)
        # Write the message to the database
        session = self.storageDB.DBSession()
        session.query(self.storageDB.PrivateMessaging).filter_by(id=id).delete()
        session.commit()

    def make_pgp_auth(self):
        password_message = {}
        password_message['time']=str(timegm(gmtime())/60)
        password_message['broker']=self.targetbroker
        password_message['key']=self.gpg.export_keys(self.mypgpkeyid,False,minimal=False)
        password = str(self.gpg.sign(json.dumps(password_message),keyid=self.mypgpkeyid,passphrase=self.pgp_passphrase))
        return password


    def select_random_broker(self):
        transports=[]
        if self.i2p_proxy_enabled and len(self.i2p_brokers) > 0:
            transports.append('i2p')
        if self.proxy_enabled and len(self.onion_brokers) > 0:
            transports.append('tor')
        transport = random.choice(transports)
        if transport == 'i2p':
            self.targetbroker = random.choice(self.i2p_brokers)
        elif transport == 'tor':
            self.targetbroker = random.choice(self.onion_brokers)

    def new_contact(self, contact):
        if contact.pgpkey != "":
            importedkey = self.gpg.import_keys(contact.pgpkey)
            contact.pgpkeyid = str(importedkey.fingerprints[0][-16:])
            if not importedkey.count == 1 and contact.pgpkeyid:
                flash_msg = queue_task(0,'flash_error','Contact not added: unable to extract PGP key ID for ' + contact.displayname)
                self.q_res.put(flash_msg)
                return False
        elif not contact.pgpkeyid:
            return False
        flash_msg = queue_task(0,'flash_message','Added contact ' + contact.displayname + '(' + contact.pgpkeyid + ')')
        self.q_res.put(flash_msg)
        session = self.storageDB.DBSession()
        new_db_contact = self.storageDB.Contacts(
                                                    contact_name=contact.displayname,
                                                    contact_key=contact.pgpkeyid,
                                                    #
                                                 )
        session.add(new_db_contact)
        cachedkey = self.storageDB.cachePGPKeys(key_id=contact.pgpkeyid,
                                                        updated=datetime.strptime(current_time(),"%Y-%m-%d %H:%M:%S"),
                                                        keyblock=contact.pgpkey )
        session.add(cachedkey)
        session.commit()

    def new_listing(self, listing):
        if listing.title != "":
            pass
        else:
            return False
        flash_msg = queue_task(0,'flash_message','Added listing ' + listing.title)
        self.q_res.put(flash_msg)
        session = self.storageDB.DBSession()
        new_listing = self.storageDB.Listings(
                                                    id=listing.id,
                                                    title=listing.title,
                                                    category=listing.categories,
                                                    description=listing.description,
                                                    price=listing.unitprice,
                                                    currency_code = listing.currency_code,
                                                    qty_available = int(listing.quantity_available),
                                                    order_max_qty = int(listing.order_max_qty),
                                                    image_base64 = listing.image_str,
                                                    public = bool(listing.is_public),
                                                    shipping_options = listing.shipping_options
                                                    # TODO: Add other fields
                                                 )
        session.add(new_listing)
        session.commit()
        time.sleep(0.1)

    def update_listing(self, listing):
        if listing.title != "":
            pass
        else:
            return False
        session = self.storageDB.DBSession()
        print "UPDATE LISTING DB " + listing.shipping_options
        db_listing = session.query(self.storageDB.Listings).filter_by(id=listing.id).first()
        db_listing.id=listing.id
        db_listing.title=listing.title
        db_listing.category=listing.categories
        db_listing.description=listing.description
        db_listing.price=listing.unitprice
        db_listing.currency_code = listing.currency_code
        db_listing.qty_available = int(listing.quantity_available)
        db_listing.order_max_qty = int(listing.order_max_qty)
        db_listing.image_base64 = listing.image_str
        db_listing.public = bool(listing.is_public)
        db_listing.shipping_options= listing.shipping_options
        session.commit()
        flash_msg = queue_task(0,'flash_message','Updated listing ' + listing.title)
        self.q_res.put(flash_msg)
        time.sleep(0.1)

    def delete_listing(self,id):
        # Write the message to the database
        session = self.storageDB.DBSession()
        session.query(self.storageDB.Listings).filter_by(id=id).delete()
        session.commit()
        flash_msg = queue_task(0,'flash_message','Deleted listing ')
        self.q_res.put(flash_msg)
        time.sleep(0.1)

    def read_configuration(self):
        # read configuration from database
        session = self.storageDB.DBSession()
        try:
            socks_proxy_enabled = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "socks_enabled").first()
            i2p_socks_proxy_enabled = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "i2p_socks_enabled").first()
            socks_proxy = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "proxy").first()
            socks_proxy_port = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "proxy_port").first()
            i2p_socks_proxy = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "i2p_proxy").first()
            i2p_socks_proxy_port = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "i2p_proxy_port").first()
            brokers = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "hubnodes").all()
            display_name = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "displayname").first()
            publish_identity = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "publish_identity").first()
            profile_text = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "profile").first()
            avatar_image = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "avatar_image").first()
            message_retention = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "message_retention").first()
            allow_unsigned = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "accept_unsigned").first()
            wallet_seed = session.query(self.storageDB.Config.value).filter(self.storageDB.Config.name == "wallet_seed").first()
        except:
            return False
        # Calculate btc master key from wallet seed
        if wallet_seed.value:
            self.btc_master_key = btc.bip32_master_key(wallet_seed.value)
        else:
            print "ERROR: Failed to generate Bitcoin master key, no seed found in database"
        # Tor SOCKS proxy
        self.proxy = socks_proxy.value
        self.proxy_port = socks_proxy_port.value
        if socks_proxy.value and socks_proxy_port.value and (socks_proxy_enabled.value == 'True'):
            self.proxy_enabled = bool(socks_proxy_enabled)
        else:
            self.proxy_enabled = False
        # i2p SOCKS proxy
        self.i2p_proxy = i2p_socks_proxy.value
        self.i2p_proxy_port = i2p_socks_proxy_port.value
        if i2p_socks_proxy.value and i2p_socks_proxy_port.value and (i2p_socks_proxy_enabled.value == 'True'):
            self.i2p_proxy_enabled = bool(i2p_socks_proxy_enabled)
        else:
            self.i2p_proxy_enabled = False
        self.brokers = brokers
        self.display_name = display_name.value
        if profile_text:
            self.profile_text = profile_text.value
        else:
            self.profile_text =''
        if avatar_image:
            self.avatar_image = avatar_image.value
        else:
            self.avatar_image =''
        self.retention_period = message_retention
        self.allow_unsigned = bool(allow_unsigned)
        if publish_identity.value:
            self.publish_identity = publish_identity
        # TODO: Read and assign all config options
        return True

    def get_items_from_listings(self,key_id,listings_dict):
        verified_listings=[]
        for item in listings_dict:
            verify_item = self.gpg.verify(item)
            item = item.replace('\n', '') # remove the textwrapping we applied when encoding this submessage
            if verify_item.key_id == key_id: # TODO - this is a weak check - check the fingerprint is set
                try:
                    stripped_item = item[item.index('{'):item.rindex('}')+1]
                except:
                    stripped_item = ''
                    print "Error: item not extracted from signed sub-message"
                    continue
                try:
                    verified_item = json.loads(stripped_item) # TODO: Additional input validation required here
                    print verified_item
                    item_shipping_options = json.loads(verified_item['shipping_options']) # TODO: Additional input validation required here
                    verified_item['shipping_options'] = item_shipping_options
                    verified_listings.append(verified_item)
                except:
                    print "Error: item json not extracted from signed sub-message"
                    print item
                    continue
                # Add this listing (or update if already present) in the listings item memory cache
                self.memcache_listing_items[key_id][str(verified_item['id'])]= (item,verified_item) # Bit shitty - tuple containing raw, signed msg & validate python object
                #print self.memcache_listing_items[key_id]
                #self.memcache_listing_items[keyid]['raw_msg']= verified_item
            else:
                print "Error: Item signture not verified for listing message held in cache db"
        return verified_listings

    def add_to_cart(self,item_id,key_id):
        #TODO: At the moment this will only work if the listing has already been retrieved (cached) - implement a fetch here if we need one
        try:
            raw_msg,msg = (self.memcache_listing_items[key_id][item_id])
        except:
            print "ERROR: item could not be added to cart because it is not cached- try viewing the item first..."
            return False
        cart_res=dict(msg)
        session = self.storageDB.DBSession()
        # TODO : error handling and input validation on this json
        item_from_msg = json.dumps(cart_res)
        cart_db_res = session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).filter(self.storageDB.Cart.item_id == cart_res['id']).count() > 0 # If it already exists then update
        if cart_db_res:
            print "Updating existing cart entry with another add"
            # TODO - since item is alrea in cart, we should add new quantity to exsintng quantity  - for now we overwrite exisiting entry
            cart_entry =  session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).filter(self.storageDB.Cart.item_id == cart_res['id']).first()
#            cart_entry = session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).filter(self.storageDB.Cart.item_id == cart_res['id']).update({
#                                                            self.storageDB.Cart.item_id:cart_res['id'],
#                                                            self.storageDB.Cart.raw_item:raw_msg,
#                                                            self.storageDB.Cart.item:item_from_msg,  # json.dumps(msg) # item:msg.__str__(),
#                                                            self.storageDB.Cart.quantity:(1 + cart_entry.quantity), # todo: add quantity to existing quantity
#                                                            self.storageDB.Cart.shipping:1
#                                                        })
            # cart_entry.iten_id = cart_res['id'] # this should not need to be updated
            cart_entry.raw_item = raw_msg
            cart_entry.item = item_from_msg
            cart_entry.quantity =  cart_entry.quantity + 1
            # cart_entry.shipping = 1 # cart_res['shipping'] # leave shipping as it is

        else:
            print "Adding new cart entry"
            cart_entry = self.storageDB.Cart(seller_key_id=key_id,
                                                        item_id=cart_res['id'],
                                                        raw_item=raw_msg,
                                                        item=item_from_msg, # json.dumps(msg), #.__str__(),
                                                        quantity=1, # ToDO read quantity from listing form if given
                                                        shipping=1) # ToDO read shipping choice from listing form if given                                                       )

            session.add(cart_entry)
        session.commit()

    def update_cart(self,item_id,key_id,quantity,shipping):
        # TODO error checking...
        session = self.storageDB.DBSession()
        cart_entry =  session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).filter(self.storageDB.Cart.item_id == item_id).first()
        cart_entry.quantity = quantity
        cart_entry.shipping = shipping
        session.commit()

    def remove_from_cart(self,key_id):
        session = self.storageDB.DBSession()
        cart_entry =  session.query(self.storageDB.Cart).filter(self.storageDB.Cart.seller_key_id == key_id).delete()
        session.commit()

    def run(self):
        # TODO: Clean up this flow
        # make db connection

        self.storageDB = Storage(self.dbsecretkey,"storage.db",self.appdir)
        if not self.storageDB.Start():
            print "Error: Unable to start storage database"
            flash_msg = queue_task(0,'flash_error','Unable to start storage database ' + 'storage.db') #' self.targetbroker)
            self.q_res.put(flash_msg)
            self.shutdown = True
        # read configuration from config table
        if not self.read_configuration():
            flash_msg = queue_task(0,'flash_error','Unable to read configuration from database ' + 'storage.db') #' self.targetbroker)
            self.q_res.put(flash_msg)
            self.shutdown = True
        # TODO: Execute database weeding functions here to include:
        # 1 - purge all PM's older than the configured retention period (unless message has been marked for retention)
        # 2 - purge addresses (buyer & seller side) for address information related to finalized transactions
        # -------------------------
        # COnfirm proxy settings

        # sort the broker list into Tor, i2p and clearnet
        for broker in self.brokers:
            broker = broker[0]
            if str(broker).endswith('.onion'):
                self.onion_brokers.append(broker)
            elif str(broker).endswith('.b32.i2p'):
                self.i2p_brokers.append(broker)
            else:   # There is a broker than appears to be neither Tor or i2p - we will not process these further for now unless test mode is enabled
                # TODO: check for test mode and permit clearnet RFC1918 addresses only if it is enabled - right now these will be ignored
                self.clearnet_brokers.append(broker)
        if (not self.proxy_enabled) and (not self.i2p_proxy_enabled):
                flash_msg = queue_task(0,'flash_error','WARNING: No Tor or i2p proxy specified. Setting off-line mode for your safety')
                self.q_res.put(flash_msg)
                self.workoffline = True
        # self.targetbroker = random.choice(self.brokers).value
        if not self.workoffline:
            # Select a random broker from our list of entry points and make mqtt connection
            self.select_random_broker()
            if self.targetbroker.endswith('.onion'):
                client = mqtt(self.mypgpkeyid,False,proxy=self.proxy,proxy_port=int(self.proxy_port))
                print self.proxy
                print self.proxy_port
                flash_msg = queue_task(0,'flash_message','Connecting to Tor hidden service ' + self.targetbroker)
                self.q_res.put(flash_msg)
            elif self.targetbroker.endswith('.b32.i2p'):
                client = mqtt(self.mypgpkeyid,False,proxy=self.i2p_proxy,proxy_port=int(self.i2p_proxy_port))
                print self.i2p_proxy
                print self.i2p_proxy_port
                flash_msg = queue_task(0,'flash_message','Connecting to i2p hidden service ' + self.targetbroker)
                self.q_res.put(flash_msg)
            else: # TODO: Only if in test mode
                if self.test_mode == True:
                    client = mqtt(self.mypgpkeyid,False,proxy=None,proxy_port=None)
                flash_msg = queue_task(0,'flash_error','WARNING: On-line mode enabled and target broker does not appear to be a Tor or i2p hidden service')
                self.q_res.put(flash_msg)
            # client = mqtt.Client(self.mypgpkeyid,False) # before custom mqtt client # original paho-mqtt
            client.on_connect= self.on_connect
            client.on_message = self.on_message
            client.on_disconnect = self.on_disconnect
            client.on_publish = self.on_publish
            client.on_subscribe = self.on_subscribe
            # create broker authentication request
            password=self.make_pgp_auth()
            # print password
            client.username_pw_set(self.mypgpkeyid,password)
            flash_msg = queue_task(0,'flash_status','Connecting...')
            self.q_res.put(flash_msg)
            try:
                self.connected = False
                #client.connect_async(self.targetbroker, 1883, 60)  # This is now async
                client.connect(self.targetbroker, 1883, 60)  # This is now async
#                time.sleep(0.5) # TODO: Find a better way to prevent the disconnect/reconnection loop following a connect

            except: # TODO: Async connect now means this error code will need to go elsewhere
                flash_msg = queue_task(0,'flash_error','Unable to connect to broker ' + self.targetbroker + ', retrying...')
                self.q_res.put(flash_msg)
                pass
        while not self.shutdown:
            if not self.workoffline:
                    client.loop(0.05) # deal with mqtt events
            time.sleep(0.05)
#            if not self.connected and not self.workoffline and 1==0: # TODO - sort this!!
#                try:
#                    # create broker authentication request
#                    flash_msg = queue_task(0,'flash_status','Connecting...')
#                    self.q_res.put(flash_msg)
#                    password=self.make_pgp_auth()
#                    client.username_pw_set(self.mypgpkeyid,password)
#                    client.connect(self.targetbroker, 1883, 60) # todo: make this connect_async
#                    time.sleep(0.5) # TODO: Find a better way to prevent the disconnect/reconnection loop following a connect
#                    self.connected = True
#                except:
#                    # print "Could not connect to broker, will retry (main loop)"
#                    print "reconnect failed"
#                    pass

            for pending_message in self.task_state_messages.keys():   # check any messages queued for whatever reason
                if self.task_state_messages[pending_message]['message'].recipient == self.mypgpkeyid or self.task_state_messages[pending_message]['message'].recipient == "":
                    outbound=False
                    pending_key=self.task_state_messages[pending_message]['message'].sender
                else:
                    outbound=True
                    pending_key=self.task_state_messages[pending_message]['message'].recipient
                pending_message_state = self.task_state_messages[pending_message]['state']
                # check pending message state
                if pending_message_state == MSG_STATE_NEEDS_KEY:
                    print "Need to request key " + pending_key
                    self.task_state_pgpkeys[pending_key]['state']=KEY_LOOKUP_STATE_INITIAL # create task request for a lookup
                    self.task_state_messages[pending_message]['state']=MSG_STATE_KEY_REQUESTED
                elif pending_message_state == MSG_STATE_KEY_REQUESTED:
#                    print "Message is waiting on a key " + pending_key
                    if self.task_state_pgpkeys[pending_key]['state']==KEY_LOOKUP_STATE_FOUND:
                        self.task_state_messages[pending_message]['state']=MSG_STATE_READY_TO_PROCESS
#                        del self.task_state_pgpkeys[pending_key]
                elif pending_message_state == MSG_STATE_QUEUED:
                    print "Can we send queued message for " + pending_key
                    if self.connected:
                        if outbound: # this should always be true as incoming messages should never be set to MSG_STATE_QUEUED
                            self.sendMessage(client,self.task_state_messages[pending_message]['message'])
                            self.task_state_messages[pending_message]['state']=MSG_STATE_DONE
                elif pending_message_state == MSG_STATE_READY_TO_PROCESS:
                    print "Deferred message now ready"
                    if outbound:
                        print "Sending deferred message"
                        self.sendMessage(client,self.task_state_messages[pending_message]['message'])
                        self.task_state_messages[pending_message]['state']=MSG_STATE_DONE
                    else:
                        # This is an inbound message - throw it back at getmessage()
                        print "Re-processing received deferred message"
                        self.task_state_messages[pending_message]['state']=MSG_STATE_DONE
                        msg = MQTTMessage()
                        msg.payload = self.task_state_messages[pending_message]['message'].raw_message
                        msg.topic = self.task_state_messages[pending_message]['message'].topic
                        print msg.topic
                        self.on_message(client,None,msg)
#                        self.myMessaging.GetMessage(self.task_state_messages[pending_message]['message'].raw_message,self,allow_unsigned=self.allow_unsigned)

                elif pending_message_state == MSG_STATE_DONE:
                    del self.task_state_messages[pending_message]

            for pgp_key in self.task_state_pgpkeys.keys():   # initiate & monitor pgp key requests
                try:
                    state = self.task_state_pgpkeys[pgp_key]['state']
                except KeyError:
                    state = None
                if state == KEY_LOOKUP_STATE_INITIAL:
                    key_topic = 'user/' + pgp_key + '/key'
                    res = client.subscribe(str(key_topic),1)
                    if res[0] == MQTT_ERR_SUCCESS:
                        self.task_state_pgpkeys[pgp_key]['state'] = KEY_LOOKUP_STATE_REQUESTED
                        print "Subscribing to requested PGP key topic " + key_topic + " ...Subscribe Done"
                    else:
                        print "Subscribing to requested PGP key topic " + key_topic + " ...Subscribe Failed"
#                elif state == KEY_LOOKUP_STATE_REQUESTED:
#                    print "Waiting for key..."
#                elif state == KEY_LOOKUP_STATE_FOUND:
#                    print "Got key."
                elif state == KEY_LOOKUP_STATE_NOTFOUND:
                    print "Could not find a key OR unable to retrieve key"

            if not self.q.empty():
                task = self.q.get()
                if task.command == 'send_pm':
                    message = Message()
                    message.type = 'Private Message'
                    message.sender = self.mypgpkeyid
                    message.recipient = task.data['recipient']
                    message.subject = task.data['subject']
                    message.body = task.data['body']
                    message.sent = False
                    self.sendMessage(client,message)
                elif task.command == 'delete_pm':
                    message_to_del = task.data['id']
                    self.delete_pm(message_to_del)
                elif task.command == 'get_key': # fetch a key from a user
                    print "Client Requesting key from backend"
                    self.task_state_pgpkeys[task.data['keyid']]['state']=KEY_LOOKUP_STATE_INITIAL # create task request for a lookup
#                    key_topic = 'user/' + task.data['keyid'] + '/key'
#                    client.subscribe(str(key_topic),1) # disabked 24th July as duplicating code in the state_table
                elif task.command == 'get_profile':
                    key_topic = 'user/' + task.data['keyid'] + '/profile'
                    client.subscribe(str(key_topic),1)
                    print "Requesting profile for " + task.data['keyid']
                elif task.command == 'get_listings':
                    item_id = task.data['id']
                    key_id = task.data['keyid']
                    print "Get_listings command received : " + key_id + "/" + item_id
                    if item_id == 'none':
                        # no item - extract all listings
                        session = self.storageDB.DBSession()
                        cachelistings = session.query(self.storageDB.cacheListings).filter_by(key_id=key_id).first()
                        if not cachelistings: # no existing listings found in cache, request it
                            key_topic = 'user/' + key_id + '/items'
                            client.subscribe(str(key_topic),1)
                            print "Requesting listings for " + key_id
                        else: # we have returned an existing listings from the cache - check age of cached listings
                            if get_age(cachelistings.updated) > CACHE_EXPIRY_LIMIT:
                                # cached listings too old - get latest listings
                                # TODO - return cached listing if we have one and flash a message telling user it is cached and a background update has been initiated

                                if self.workoffline:
                                    flash_msg = queue_task(0,'flash_message','Cached listings for this seller have expired, you should go online to get the latest listings.')
                                    print "Extracting listings from cached copy"
    #                                verified_listings=[]
                                    listings_dict = json.loads(cachelistings.listings_block)
                                    if not listings_dict:
                                        print "No items in the cached listings block for " + key_id
                                        self.q_data.put ('none')
                                    else:
                                        verified_listings = self.get_items_from_listings(key_id,listings_dict)
                                        self.q_data.put (verified_listings)
                                else:
                                    flash_msg = queue_task(0,'flash_message','Cached listings for this seller have expired, the latest listings have been requested in the background. Refresh the page.')
                                    self.q_res.put(flash_msg)
                                    self.q_res.put(flash_msg)
                                    key_topic = 'user/' + key_id + '/items'
                                    client.subscribe(str(key_topic),1)
                                    print "Requesting updated listings for " + key_id
                            else:
                                # we have an unexpired cacehd copy of this listing so just return listings from db -
                                print "Request for listings ignored...we have a copy in cache that has not expired..."
                                print "Extracting listings from cached copy"
#                                verified_listings=[]
                                listings_dict = json.loads(cachelistings.listings_block)
                                if not listings_dict:
                                    print "No items in the cached listings block for " + key_id
                                    self.q_data.put ('none')
                                else:
                                    verified_listings = self.get_items_from_listings(key_id,listings_dict)
                                    self.q_data.put (verified_listings)
                    else:
                    # Looks like user is viewing a specific item
                    #TODO when a user attempts to view an item without first retrieving listings it shows as blank, user must refresh - FIX IT
                        try:
                            #print self.memcache_listing_items
                            #print self.memcache_listing_items[key_id]
                            if not self.memcache_listing_items[key_id][item_id] == '':
                                print "Entry found in listing item memcache..."
                                # TODO: Find casue of sporadic socket error serving the response for this back to the browser - its probably an exception here
#                                print self.memcache_listing_items[key_id][item_id][1] # [0] is the raw, signed copy of the listiing, [1] is stripped
                                self.q_data.put (self.memcache_listing_items[key_id][item_id][1])
                        except KeyError:
                            print "Could not find  memcache entry for " + str(key_id + ' ' + item_id)
                            # Try refreshing listings for this user (this can happen when the user clicks a direct link to an item without viewing listigns first)
                            key_topic = 'user/' + key_id + '/items'
                            client.subscribe(str(key_topic),1)
                            print "Requesting updated listings for " + key_id

                elif task.command == 'add_to_cart':
                    item_id = task.data['item_id']
                    key_id = task.data['key_id']
                    print "Backend received add to cart request for " + key_id + '/' + item_id
                    self.add_to_cart(item_id,key_id)

                elif task.command == 'update_cart':
                    item_id = task.data['item_id']
                    key_id = task.data['key_id']
                    quantity = task.data['quantity']
                    shipping = task.data['shipping']
                    print "Backend received update cart request for " + key_id + '/' + item_id
                    self.update_cart(item_id,key_id,quantity,shipping)

                elif task.command == 'remove_from_cart':
                    key_id = task.data['key_id']
                    print "Backend received delete from cart request for items from seller " + key_id
                    self.remove_from_cart(key_id)

                elif task.command == 'publish_listings':
                    listings_out_message = Messaging.PrepareMessage(self.myMessaging,self.create_listings_msg())
                    client.publish(self.pub_items,listings_out_message,1,True)      # Our published items queue, qos=1, durable
                    flash_msg = queue_task(0,'flash_message','Re-publishing listings')
                    self.q_res.put(flash_msg)
                elif task.command == 'get_directory':
                        # Request list of all users
                        key_topic = 'user/+/directory'
                        client.subscribe(str(key_topic),1)
                        print "Requesting directory of users"
                elif task.command == 'new_contact':
                    contact = Contact()
                    contact.displayname = task.data['displayname']
                    contact.pgpkey = task.data['pgpkey']
                    contact.pgpkeyid = task.data['pgpkeyid']
                    contact.flags = ''#task.data['flags']
                    self.new_contact(contact)
                elif task.command == 'new_listing':
                    print "New listing: " + task.data['title']
                    listing = Listing()
                    listing.title=task.data['title']
                    listing.categories=task.data['category']
                    listing.description=task.data['description']
                    listing.unitprice=task.data['price']
                    listing.currency_code=task.data['currency']
                    listing.image_str=task.data['image']
                    listing.is_public=task.data['is_public']
                    listing.quantity_available=task.data['quantity']
                    listing.order_max_qty=task.data['max_order']
                    listing.shipping_options=task.data['shipping_options']
                    #TODO: add other listing fields
                    self.new_listing(listing)
                elif task.command == 'update_listing':
                    print "Update listing: " + task.data['title']
                    listing = Listing()
                    listing.id=task.data['id'] # Since we are updating the generate id needs to be overwritten
                    listing.title=task.data['title']
                    listing.categories=task.data['category']
                    listing.description=task.data['description']
                    listing.unitprice=task.data['price']
                    listing.currency_code=task.data['currency']
                    listing.image_str=task.data['image']
                    listing.is_public=task.data['is_public']
                    listing.quantity_available=task.data['quantity']
                    listing.order_max_qty=task.data['max_order']
                    listing.shipping_options=task.data['shipping_options']
                    #TODO: add other listing fields
                    self.update_listing(listing)
                elif task.command == 'delete_listing':
                    listing_to_del = task.data['id']
                    self.delete_listing(listing_to_del)
                elif task.command == 'shutdown':
                    self.shutdown = True
        try:
            client
        except NameError:
            pass
        else:
            if client._state == mqtt_cs_connected:
                client.disconnect()
        self.storageDB.DBSession.close_all()
        print "client-backend exits"
        # Terminated
