import base64
import Queue

from crypto.crypto import Crypto

from message import Message

from threading import Thread

from utils import constants
from utils import errors
from utils import exceptions
from utils import utils


class Client(Thread):
    def __init__(self, connectionManager, remoteNick, crypto, sendMessageCallback, recvMessageCallback, handshakeDoneCallback, errorCallback, initiateHandkshakeOnStart=False):
        Thread.__init__(self)
        self.daemon = True

        self.connectionManager = connectionManager
        self.remoteNick = remoteNick
        self.sendMessageCallback = sendMessageCallback
        self.recvMessageCallback = recvMessageCallback
        self.handshakeDoneCallback = handshakeDoneCallback
        self.errorCallback = errorCallback
        self.initiateHandkshakeOnStart = initiateHandkshakeOnStart

        self.isEncrypted = False
        self.wasHandshakeDone = False
        self.messageQueue = Queue.Queue()

        self.crypto = crypto
        self.crypto.generateDHKey()


    def sendChatMessage(self, text):
        self.sendMessage(constants.COMMAND_MSG, text)


    def sendMessage(self, command, payload=None):
        message = Message(clientCommand=command, destNick=self.remoteNick)

        # Encrypt all outgoing data
        if payload is not None and self.isEncrypted:
            payload = self.crypto.aesEncrypt(payload)
            message.setEncryptedPayload(payload)

            # Generate and set the HMAC for the message
            message.setBinaryHmac(self.crypto.generateHmac(payload))
        else:
            message.payload = payload

        self.sendMessageCallback(message)


    def postMessage(self, message):
        self.messageQueue.put(message)


    def run(self):
        if self.initiateHandkshakeOnStart:
            self.__initiateHandshake()
        else:
            self.__doHandshake()

        while True:
            message = self.messageQueue.get()

            command = message.clientCommand
            payload = message.payload

            # Check if the client requested to end the connection
            if command == constants.COMMAND_END:
                self.connectionManager.destroyClient(self.remoteNick)
                self.errorCallback(self.remoteNick, errors.ERR_CONNECTION_ENDED)
                return
            # Ensure we got a valid command
            elif self.wasHandshakeDone and command not in constants.LOOP_COMMANDS:
                self.connectionManager.destroyClient(self.remoteNick)
                self.errorCallback(self.remoteNick, errors.ERR_INVALID_COMMAND)
                return

            # Decrypt the incoming data
            payload = self.__getDecryptedPayload(message)

            self.messageQueue.task_done()
            self.recvMessageCallback(command, message.sourceNick, payload)


    def connect(self):
        self.__initiateHandshake()


    def disconnect(self):
        try:
            self.sendMessage(constants.COMMAND_END)
        except Exception:
            pass


    def __doHandshake(self):
        try:
            # The caller of this function (should) checks for the initial HELO command

            # Send the ready command
            self.sendMessage(constants.COMMAND_REDY)

            # Receive the client's public key
            clientPublicKey = self.__getHandshakeMessagePayload(constants.COMMAND_PUBLIC_KEY)
            self.crypto.computeDHSecret(long(base64.b64decode(clientPublicKey)))

            # Send our public key
            publicKey = base64.b64encode(str(self.crypto.getDHPubKey()))
            self.sendMessage(constants.COMMAND_PUBLIC_KEY, publicKey)

            # Switch to AES encryption for the remainder of the connection
            self.isEncrypted = True

            self.wasHandshakeDone = True
            self.handshakeDoneCallback(self.remoteNick)
        except exceptions.ProtocolEnd:
            self.disconnect()
            self.connectionManager.destroyClient(self.remoteNick)
        except exceptions.ProtocolError as pe:
            self.__handleHandshakeError(pe)


    def __initiateHandshake(self):
        try:
            # Send the hello command
            self.sendMessage(constants.COMMAND_HELO)

            # Receive the redy command
            self.__getHandshakeMessagePayload(constants.COMMAND_REDY)

            # Send our public key
            publicKey = base64.b64encode(str(self.crypto.getDHPubKey()))
            self.sendMessage(constants.COMMAND_PUBLIC_KEY, publicKey)

            # Receive the client's public key
            clientPublicKey = self.__getHandshakeMessagePayload(constants.COMMAND_PUBLIC_KEY)
            self.crypto.computeDHSecret(long(base64.b64decode(clientPublicKey)))

            # Switch to AES encryption for the remainder of the connection
            self.isEncrypted = True

            self.wasHandshakeDone = True
            self.handshakeDoneCallback(self.remoteNick)
        except exceptions.ProtocolEnd:
            self.disconnect()
            self.connectionManager.destroyClient(self.remoteNick)
        except exceptions.ProtocolError as pe:
            self.__handleHandshakeError(pe)


    def __getHandshakeMessagePayload(self, expectedCommand):
        message = self.messageQueue.get()

        if message.clientCommand != expectedCommand:
            if message.clientCommand == constants.COMMAND_END:
                raise exceptions.ProtocolEnd
            elif message.clientCommand == constants.COMMAND_REJECT:
                raise exceptions.ProtocolError(errors.ERR_CONNECTION_REJECTED)
            else:
                raise exceptions.ProtocolError(errors.ERR_BAD_HANDSHAKE)

        payload = self.__getDecryptedPayload(message)

        self.messageQueue.task_done()
        return payload


    def __getDecryptedPayload(self, message):
        if self.isEncrypted:
            payload = message.getEncryptedPayloadAsBinaryString()

            # Check the HMAC
            if not self.__verifyHmac(message.hmac, payload):
                self.errorCallback(message.sourceNick, errors.ERR_BAD_HMAC)
                raise exceptions.CryptoError(errors.BAD_HMAC)

            try:
                # Decrypt the payload
                payload = self.crypto.aesDecrypt(payload)
            except exceptions.CryptoError as ce:
                self.errorCallback(message.sourceNick, errors.ERR_BAD_DECRYPT)
                raise ce
        else:
            payload = message.payload

        return payload


    def __verifyHmac(self, givenHmac, payload):
        generatedHmac = self.crypto.generateHmac(payload)
        return utils.secureStrcmp(generatedHmac, base64.b64decode(givenHmac))


    def __handleHandshakeError(self, exception):
        self.errorCallback(self.remoteNick, exception.errno)

        # For all errors except the connection being rejected, tell the client there was an error
        if hasattr(exception, 'errno') and exception.errno != errors.ERR_CONNECTION_REJECTED:
            self.sendMessage(constants.COMMAND_ERR)
        # For reject errors, delete this client
        else:
            self.connectionManager.destroyClient(self.remoteNick)
