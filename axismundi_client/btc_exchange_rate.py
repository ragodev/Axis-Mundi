import threading
from utilities import queue_task
#import requests
import urllib2
import json
from time import sleep
import socks
from sockshandler import SocksiPyHandler
import random
import os

class btc_exchange_rate(threading.Thread):
    # Thread to handle periodic exchange rate lookups. Queries multiple sources in case of failure
    # Requires a SOCKS proxy to be provided - by default the Tor SOCKS proxy is used

    def __init__(self, socks_proxy, socks_port, queue):
        self.socks_proxy = socks_proxy
        self.socks_port = socks_port
        self.queue = queue
        self.running = True
        threading.Thread.__init__(self)

    def run(self):
        print "Info: BTC Exchange rate thread started using SOCKS proxy " + self.socks_proxy + ":" + self.socks_port
        # Make the request look like it came from a browser TODO - define the browser headers elsewhere so they can be easily updated
        headers = [('User-Agent','Mozilla/5.0 (Windows NT 6.1; rv:31.0) Gecko/20100101 Firefox/31.0'),
                   ('Accept','text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'),
                   ('Accept-Language','en-us,en;q=0.5')]

        os.environ['no_proxy']='127.0.0.1'  # This needs to be set to avoid a hard crash on OSX when using urllib or requests

        opener = urllib2.build_opener(SocksiPyHandler(socks.SOCKS5, self.socks_proxy, int(self.socks_port)))
        opener.addheaders = headers

        BTC_API_BITPAY = 'http://bitpay.com/api/rates'
        BTC_API_BITCOINCHARTS = 'http://api.bitcoincharts.com/v1/weighted_prices.json'
        BTC_API_BITCOINAVERAGE = 'https://api.bitcoinaverage.com/ticker/global/all'
        sources = [BTC_API_BITPAY,BTC_API_BITCOINCHARTS]
        source_index = 0
        while self.running:
            source = sources[source_index]# BTC_API_BITCOINCHARTS
            try:
                response = opener.open(source,None,30).read()
                #print "Response" + str(response)
                data_list = json.loads(response)
                # Process based on source
                if source == BTC_API_BITPAY:
                    data = data_list # we can use these results as they come
                elif source == BTC_API_BITCOINCHARTS:
                    data = []
                    for code in data_list:
                        if not (code == 'CHF' or code == 'timestamp'): # For some reasson CHF is broken in the bitcoincharts api
                            data.append({'code': code,'rate':data_list[code]['24h']})
                elif source == BTC_API_BITCOINAVERAGE: # TODO - Process bitcoinaverage api output
                    print "BTC_API_BITCOINAVERAGE NOT IMPLEMENTED"
                    data = []
                    for code in data_list:
                        data.append({'code': code,'rate':data_list[code]['24h_avg']})

                task = queue_task(1, 'update_exchange_rates', data)
                print "Info: Exchange rates updated OK using " + source
                self.queue.put(task)
                sleep(600 + random.randint(0, 1200)) # refresh every 10 - 30 minutes - randomize period
            except:
                print "Warning: Failed to retrieve current exchange rates from " + source
                if source_index < len(sources)-1:
                    source_index += 1
                else:
                    source_index = 0
                sleep (10) # try again in 10 seconds


        print "Exchange rate collector shutting down"