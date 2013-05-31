#!/usr/bin/env python
#
#Copyright (c) 2013, Toshiaki Hatano <haeena@haeena.net>
#
#Contain code from the eve-bot
#Copyright (c) 2009, Philip Cass <frymaster@127001.org>
#Copyright (c) 2009, Alan Ainsworth <fruitbat@127001.org>
#
#Contains code from the Mumble Project:
#Copyright (C) 2005-2009, Thorvald Natvig <thorvald@natvig.com>
#
#All rights reserved.
#
#Redistribution and use in source and binary forms, with or without
#modification, are permitted provided that the following conditions
#are met:
#
#- Redistributions of source code must retain the above copyright notice,
#  this list of conditions and the following disclaimer.
#- Redistributions in binary form must reproduce the above copyright notice,
#  this list of conditions and the following disclaimer in the documentation
#  and/or other materials provided with the distribution.
#- Neither the name of localhost, 127001.org, eve-bot nor the names of its
#  contributors may be used to endorse or promote products derived from this
#  software without specific prior written permission.

#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE FOUNDATION OR
#  CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
#  EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
#  PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
#  PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
#  LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
#  NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#  SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import socket
import time
import struct
import sys
import select
import collections
import thread
import threading
import signal
import os
import optparse
import platform

import pdb
from pprint import pprint
#The next 2 imports may not succeed
warning=""
try:
    import ssl
except:
    warning+="WARNING: This python program requires the python ssl module (available in python 2.6; standalone version may be at found http://pypi.python.org/pypi/ssl/)\n"
try:
    import Mumble_pb2
except:
    warning+="WARNING: Module Mumble_pb2 not found\n"
    warning+="This program requires the Google Protobuffers library (http://code.google.com/apis/protocolbuffers/) to be installed\n"
    warning+="You must run the protobuf compiler \"protoc\" on the Mumble.proto file to generate the Mumble_pb2 file\n"
    warning+="Move the Mumble.proto file from the mumble source code into the same directory as this bot and type \"protoc --python_out=. Mumble.proto\"\n"

headerFormat=">HI"
eavesdropper=None
messageLookupMessage={Mumble_pb2.Version:0,Mumble_pb2.UDPTunnel:1,Mumble_pb2.Authenticate:2,Mumble_pb2.Ping:3,Mumble_pb2.Reject:4,Mumble_pb2.ServerSync:5,
        Mumble_pb2.ChannelRemove:6,Mumble_pb2.ChannelState:7,Mumble_pb2.UserRemove:8,Mumble_pb2.UserState:9,Mumble_pb2.BanList:10,Mumble_pb2.TextMessage:11,Mumble_pb2.PermissionDenied:12,
        Mumble_pb2.ACL:13,Mumble_pb2.QueryUsers:14,Mumble_pb2.CryptSetup:15,Mumble_pb2.ContextActionAdd:16,Mumble_pb2.ContextAction:17,Mumble_pb2.UserList:18,Mumble_pb2.VoiceTarget:19,
        Mumble_pb2.PermissionQuery:20,Mumble_pb2.CodecVersion:21}
messageLookupNumber={}
threadNumber=0

for i in messageLookupMessage.keys():
        messageLookupNumber[messageLookupMessage[i]]=i

def discontinue_processing(signl, frme):
    print time.strftime("%a, %d %b %Y %H:%M:%S +0000"), "Received shutdown notice"
    if eavesdropper:
        eavesdropper.wrapUpThread()
    else:
        sys.exit(0)

signal.signal( signal.SIGINT, discontinue_processing )
#signal.signal( signal.SIGQUIT, discontinue_processing )
signal.signal( signal.SIGTERM, discontinue_processing )

class timedWatcher(threading.Thread):
    def __init__(self, plannedPackets,socketLock,socket):
        global threadNumber
        threading.Thread.__init__(self)
        self.plannedPackets=plannedPackets
        self.pingTotal=1
        self.isRunning=True
        self.socketLock=socketLock
        self.socket=socket
        i = threadNumber
        threadNumber+=1
        self.threadName="Thread " + str(i)

    def stopRunning(self):
        self.isRunning=False

    def run(self):
        self.nextPing=time.time()-1

        while self.isRunning:
            t=time.time()
            if t>self.nextPing:
                pbMess = Mumble_pb2.Ping()
                pbMess.timestamp=(self.pingTotal*5000000)
                pbMess.good=0
                pbMess.late=0
                pbMess.lost=0
                pbMess.resync=0
                pbMess.udp_packets=0
                pbMess.tcp_packets=self.pingTotal
                pbMess.udp_ping_avg=0
                pbMess.udp_ping_var=0.0
                pbMess.tcp_ping_avg=50
                pbMess.tcp_ping_var=50
                self.pingTotal+=1
                packet=struct.pack(headerFormat,3,pbMess.ByteSize())+pbMess.SerializeToString()
                self.socketLock.acquire()
                while len(packet)>0:
                    sent=self.socket.send(packet)
                    packet = packet[sent:]
                self.socketLock.release()
                self.nextPing=t+5
            if len(self.plannedPackets) > 0:
                if t > self.plannedPackets[0][0]:
                    self.socketLock.acquire()
                    while t > self.plannedPackets[0][0]:
                        event = self.plannedPackets.popleft()
                        packet = event[1]
                        while len(packet)>0:
                            sent=self.socket.send(packet)
                            packet = packet[sent:]
                        if len(self.plannedPackets)==0:
                            break
                    self.socketLock.release()
            sleeptime = 10
            if len(self.plannedPackets) > 0:
                sleeptime = self.plannedPackets[0][0]-t
            altsleeptime=self.nextPing-t
            if altsleeptime < sleeptime:
                sleeptime = altsleeptime
            if sleeptime > 0:
                time.sleep(sleeptime)
        print time.strftime("%a, %d %b %Y %H:%M:%S +0000"),self.threadName,"timed thread going away"                    

class mumbleConnection(threading.Thread):
    def __init__(self,host=None,nickname=None,channel=None,delay=None,limit=None,password=None,verbose=False):
        global threadNumber
        i = threadNumber
        threadNumber+=1
        self.threadName="Thread " + str(i)
        threading.Thread.__init__(self)
        self.plannedPackets=collections.deque()
        tcpSock=socket.socket(type=socket.SOCK_STREAM)
        self.socketLock=thread.allocate_lock()
        self.socket=ssl.wrap_socket(tcpSock,ssl_version=ssl.PROTOCOL_TLSv1)
        self.socket.setsockopt(socket.SOL_TCP,socket.TCP_NODELAY,1)
        self.host=host
        self.nickname=nickname
        self.channel=channel
        self.inChannel=False
        self.session=None
        self.channelId=None
        self.victimSession=None
        self.userList={}
        self.readyToClose=False
        self.timedWatcher = None
        # TODO: Implement delay and rate limit
        self.delay=delay
        self.limit=limit
        self.password=password
        self.verbose=verbose
    
    def packageMessageForSending(self,msgType,stringMessage):
        length=len(stringMessage)
        return struct.pack(headerFormat,msgType,length)+stringMessage
    
    def sendTotally(self,message):
        self.socketLock.acquire()
        while len(message)>0:
            sent=self.socket.send(message)
            if sent < 0:
                print time.strftime("%a, %d %b %Y %H:%M:%S +0000"),self.threadName,"Server socket error while trying to write, immediate abort"
                self.socketLock.release()
                return False
            message=message[sent:]
        self.socketLock.release()
        return True

    def readTotally(self,size):
        message=""
        while len(message)<size:
            received=self.socket.recv(size-len(message))
            message+=received
            if len(received)==0:
                print time.strftime("%a, %d %b %Y %H:%M:%S +0000"),self.threadName,"Server socket died while trying to read, immediate abort"
                return None
        return message
    
    def parseMessage(self,msgType,stringMessage):
        msgClass=messageLookupNumber[msgType]
        message=msgClass()
        message.ParseFromString(stringMessage)
        return message
    
    def joinChannel(self):
        if self.channelId!=None and self.session!=None:
            pbMess = Mumble_pb2.UserState()
            pbMess.session=self.session
            pbMess.channel_id=self.channelId
            if not self.sendTotally(self.packageMessageForSending(messageLookupMessage[type(pbMess)],pbMess.SerializeToString())):
                self.wrapUpThread()
                return
            self.inChannel=True

    def wrapUpThread(self):
        #called after thread is confirmed to be needing to die because of kick / socket close
        self.readyToClose=True
        self.plannedPackets=collections.deque()
    
    def readPacket(self):
        meta=self.readTotally(6)
        if not meta:
            self.wrapUpThread()
            return
        msgType,length=struct.unpack(headerFormat,meta)
        stringMessage=self.readTotally(length)
        if not stringMessage:
            self.wrapUpThread()
            return
        
        #Type 5 = ServerSync
        if (not self.session) and msgType==5 and (not self.inChannel):
            message=self.parseMessage(msgType,stringMessage)
            self.session=message.session
            self.joinChannel()
        #Type 7 = ChannelState
        if (not self.inChannel) and msgType==7 and self.channelId==None:
            message=self.parseMessage(msgType,stringMessage)
            if message.name==self.channel:
                self.channelId=message.channel_id
                self.joinChannel()
        #Type 8 = UserRemove (kick)
        if msgType==8:
            message=self.parseMessage(msgType,stringMessage)
            if self.session!=None:
                if message.session==self.session:
                    print time.strftime("%a, %d %b %Y %H:%M:%S +0000"),self.threadName,"********* KICKED ***********"
                    self.wrapUpThread()
                    return
            session=message.session
            if session in self.userList:
                del self.userList[session]
        #Type 9 = UserState
        if msgType==9:
            message=self.parseMessage(msgType,stringMessage)
            session=message.session
            if session in self.userList:
                record=self.userList[session]
            else:
                record={"session":session}
                self.userList[session]=record
            name=None
            channel=None
            if message.HasField("name"):
                name=message.name
                record["name"]=name
            if message.HasField("channel_id"):
                channel=message.channel_id
                record["channel"]=channel
            if name and not channel:
                record["channel"]=0
        #Type 11 = TextMessage
        if msgType==11:
            message=self.parseMessage(msgType,stringMessage)
            if message.actor!=self.session:
                if message.message.startswith("/vod"):
                    pbMess =  Mumble_pb2.TextMessage()
                    pbMess.actor = self.session
                    pbMess.channel_id.append(self.channelId)
                    pbMess.message = "Oreno VOD" # TODO: Koko VOD URL ni suru
                    pbMess.session.append(self.session)
                    if not self.sendTotally(self.packageMessageForSending(messageLookupMessage[type(pbMess)],pbMess.SerializeToString())):
                        self.wrapUpThread()
                    return
    
    def run(self):
        pdb.set_trace()
        try:
            self.socket.connect(self.host)
        except:
            print time.strftime("%a, %d %b %Y %H:%M:%S +0000"),self.threadName,"Couldn't connect to server"
            return
        self.socket.setblocking(0)
        print time.strftime("%a, %d %b %Y %H:%M:%S +0000"),self.threadName,"connected to server"
        pbMess = Mumble_pb2.Version()
        pbMess.release="1.2.0"
        pbMess.version=66048
        pbMess.os=platform.system()
        pbMess.os_version="mumblebee0.0.1"
        
        initialConnect=self.packageMessageForSending(messageLookupMessage[type(pbMess)],pbMess.SerializeToString())
        
        pbMess = Mumble_pb2.Authenticate()
        pbMess.username=self.nickname
        if self.password!=None:
            pbMess.password=self.password
        celtversion=pbMess.celt_versions.append(-2147483637)
        
        initialConnect+=self.packageMessageForSending(messageLookupMessage[type(pbMess)],pbMess.SerializeToString())
        
        if not self.sendTotally(initialConnect):
            return
        
        sockFD=self.socket.fileno()
        
        self.timedWatcher = timedWatcher(self.plannedPackets,self.socketLock,self.socket)
        self.timedWatcher.start()
        print time.strftime("%a, %d %b %Y %H:%M:%S +0000"),self.threadName,"started timed watcher",self.timedWatcher.threadName
        
        while True:
            pollList,foo,errList=select.select([sockFD],[],[sockFD])
            for item in pollList:
                if item==sockFD:
                    self.readPacket()
            if self.readyToClose:
                if len(self.plannedPackets)==0:
                    self.wrapUpThread()
                    break
        
        if self.timedWatcher:
            self.timedWatcher.stopRunning()
        
        self.socket.close()
        print time.strftime("%a, %d %b %Y %H:%M:%S +0000"),self.threadName,"waiting for timed watcher to die..."
        if self.timedWatcher!=None:
            while self.timedWatcher.isAlive():
                pass
        print time.strftime("%a, %d %b %Y %H:%M:%S +0000"),self.threadName,"thread going away -",self.nickname

def main():
    global eavesdropper,warning
            
    p = optparse.OptionParser(description='Mumble 1.2 chatbot',
                prog='mumblebee.py',
                version='%prog 0.0.1',
                usage='\t%prog -e \"Channel to listen to\"')
    
    p.add_option("-e","--eavesdrop-in",help="Channel to eavesdrop in (MANDATORY)",action="store",type="string")
    p.add_option("-s","--server",help="Host to connect to (default %default)",action="store",type="string",default="localhost")
    p.add_option("-p","--port",help="Port to connect to (default %default)",action="store",type="int",default=64738)
    p.add_option("-n","--nick",help="Nickname for the eavesdropper (default %default)",action="store",type="string",default="mumblebee")
    p.add_option("-d","--delay",help="Time to delay response by in seconds (default %default)",action="store",type="float",default=0)
    p.add_option("-l","--limit",help="Maximum response per minutes (default %default, 0 = unlimited)",action="store",type="int",default=0)
    p.add_option("-v","--verbose",help="Outputs and translates all messages received from the server",action="store_true",default=False)
    p.add_option("--password",help="Password for server, if any",action="store",type="string")
    
    if len(warning)>0:
        print warning
    o, arguments = p.parse_args()
    if len(warning)>0:
        sys.exit(1)
    
    if o.eavesdrop_in==None:
        p.print_help()
        print "\nYou MUST include an eavesdrop channel to listen to"
        sys.exit(1)
    
    host=(o.server,o.port)
    
    #if o.eavesdrop_in=="Root":
    #    p.print_help()
    #    print "\nEavesdrop channel cannot be root (or it would briefly attempt to mimic everyone who joined - including mimics)"
    #    sys.exit(1)
    
    eavesdropper = mumbleConnection(host,o.nick,o.eavesdrop_in,delay=o.delay,limit=o.limit,password=o.password,verbose=o.verbose)
    pp=eavesdropper.plannedPackets
    eavesdropper.start()
    
    #Need to keep main thread alive to receive shutdown signal
    
    while eavesdropper.isAlive():
        time.sleep(1)

if __name__ == '__main__':
        main()
