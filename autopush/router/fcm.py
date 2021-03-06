"""FCM Router"""
from typing import Any  # noqa

import pyfcm
from requests.exceptions import ConnectionError
from twisted.internet.threads import deferToThread
from twisted.logger import Logger

from autopush.exceptions import RouterException
from autopush.router.interface import RouterResponse
from autopush.types import JSONDict  # noqa


class FCMRouter(object):
    """FCM Router Implementation

    Note: FCM is a newer branch of GCM. While there's not much change
    required for the server, there is significant work required for the
    client. To that end, having a separate router allows the "older" GCM
    to persist and lets the client determine when they want to use the
    newer FCM route.
    """
    log = Logger()
    gcm = None
    dryRun = 0
    collapseKey = "simplepush"
    MAX_TTL = 2419200
    reasonTable = {
        "MissingRegistration": {
            "msg": ("'to' or 'registration_id' is blank or"
                    " invalid: {regid}"),
            "err": 500,
            "errno": 1,
        },
        "InvalidRegistration": {
            "msg": "registration_id is invalid: {regid}",
            "err": 410,
            "errno": 105,
        },
        "NotRegistered": {
            "msg": "device has unregistered with FCM: {regid}",
            "err": 410,
            "errno": 103,
        },
        "InvalidPackageName": {
            "msg": "Invalid Package Name specified",
            "err": 500,
            "errno": 2,
            "crit": True,
        },
        "MismatchSenderid": {
            "msg": "Invalid SenderID used: {senderid}",
            "err": 410,
            "errno": 105,
            "crit": True,
        },
        "MessageTooBig": {
            "msg": "Message length was too big: {nlen}",
            "err": 413,
            "errno": 104,
        },
        "InvalidDataKey": {
            "msg": ("Payload contains an invalid or restricted "
                    "key value"),
            "err": 500,
            "errno": 3,
            "crit": True,
        },
        "InvalidTtl": {
            "msg": "Invalid TimeToLive {ttl}",
            "err": 400,
            "errno": 111,
        },
        "Unavailable": {
            "msg": "Message has timed out or device is unavailable",
            "err": 200,
            "errno": 0,
        },
        "InternalServerError": {
            "msg": "FCM internal server error",
            "err": 500,
            "errno": 999,
        },
        "DeviceMessageRateExceeded": {
            "msg": "Too many messages for this device",
            "err": 503,
            "errno": 4,
        },
        "TopicsMessageRateExceeded": {
            "msg": "Too many subscribers for this topic",
            "err": 503,
            "errno": 5,
            "crit": True,
        },
        "Unreported": {
            "msg": "Error has no reported reason.",
            "err": 500,
            "errno": 999,
            "crit": True,
        }
    }

    def __init__(self, ap_settings, router_conf):
        """Create a new FCM router and connect to FCM"""
        self.config = router_conf
        self.min_ttl = router_conf.get("ttl", 60)
        self.dryRun = router_conf.get("dryrun", False)
        self.collapseKey = router_conf.get("collapseKey", "webpush")
        self.senderID = router_conf.get("senderID")
        self.auth = router_conf.get("auth")
        self.metrics = ap_settings.metrics
        self._base_tags = []
        try:
            self.fcm = pyfcm.FCMNotification(api_key=self.auth)
        except Exception as e:
            self.log.error("Could not instantiate FCM {ex}",
                           ex=e)
            raise IOError("FCM Bridge not initiated in main")
        self.log.debug("Starting FCM router...")
        self.ap_settings = ap_settings

    def amend_endpoint_response(self, response, router_data):
        # type: (JSONDict, JSONDict) -> None
        response["senderid"] = router_data.get('creds', {}).get('senderID')

    def register(self, uaid, router_data, app_id, *args, **kwargs):
        # type: (str, JSONDict, str, *Any, **Any) -> None
        """Validate that the FCM Instance Token is in the ``router_data``"""
        senderid = app_id
        # "token" is the GCM registration id token generated by the client.
        if "token" not in router_data:
            raise self._error("connect info missing FCM Instance 'token'",
                              status=401,
                              uri=kwargs.get('uri'),
                              senderid=repr(senderid))
        # senderid is the remote client's senderID value. This value is
        # very difficult for the client to change, and there was a problem
        # where some clients had an older, invalid senderID. We need to
        # be able to match senderID to it's corresponding auth key.
        # If the client has an unexpected or invalid SenderID,
        # it is impossible for us to reach them.
        if not (senderid == self.senderID):
            raise self._error("Invalid SenderID", status=410, errno=105)
        # Assign a senderid
        router_data["creds"] = {"senderID": self.senderID,
                                "auth": self.auth}

    def route_notification(self, notification, uaid_data):
        """Start the FCM notification routing, returns a deferred"""
        router_data = uaid_data["router_data"]
        # Kick the entire notification routing off to a thread
        return deferToThread(self._route, notification, router_data)

    def _route(self, notification, router_data):
        """Blocking FCM call to route the notification"""
        # THIS MUST MATCH THE CHANNELID GENERATED BY THE REGISTRATION SERVICE
        # Currently this value is in hex form.
        data = {"chid": notification.channel_id.hex}
        if not router_data.get("token"):
            raise self._error("No registration token found. "
                              "Rejecting message.",
                              410, errno=106, log_exception=False)
        regid = router_data.get("token")
        # Payload data is optional. The endpoint handler validates that the
        # correct encryption headers are included with the data.
        if notification.data:
            mdata = self.config.get('max_data', 4096)
            if len(notification.data) > mdata:
                raise self._error("This message is intended for a " +
                                  "constrained device and is limited " +
                                  "to 3070 bytes. Converted buffer too " +
                                  "long by %d bytes" %
                                  (len(notification.data) - mdata),
                                  413, errno=104, log_exception=False)

            data['body'] = notification.data
            data['con'] = notification.headers['encoding']
            data['enc'] = notification.headers['encryption']

            if 'crypto_key' in notification.headers:
                data['cryptokey'] = notification.headers['crypto_key']
            elif 'encryption_key' in notification.headers:
                data['enckey'] = notification.headers['encryption_key']

        # registration_ids are the FCM instance tokens (specified during
        # registration.
        router_ttl = min(self.MAX_TTL,
                         max(self.min_ttl, notification.ttl or 0))
        try:
            result = self.fcm.notify_single_device(
                collapse_key=self.collapseKey,
                data_message=data,
                dry_run=self.dryRun or ('dryrun' in router_data),
                registration_id=regid,
                time_to_live=router_ttl,
            )
        except pyfcm.errors.AuthenticationError as e:
            self.log.error("Authentication Error: %s" % e)
            raise RouterException("Server error", status_code=500)
        except ConnectionError as e:
            self.metrics.increment("updates.client.bridge.fcm.connection_err",
                                   self._base_tags)
            self.log.warn("Could not connect to FCM server: %s" % e)
            raise RouterException("Server error", status_code=502,
                                  log_exception=False)
        except Exception as e:
            self.log.error("Unhandled FCM Error: %s" % e)
            raise RouterException("Server error", status_code=500)
        self.metrics.increment("updates.client.bridge.fcm.attempted",
                               self._base_tags)
        return self._process_reply(result, notification, router_data,
                                   ttl=router_ttl)

    def _error(self, err, status, **kwargs):
        """Error handler that raises the RouterException"""
        self.log.debug(err, **kwargs)
        return RouterException(err, status_code=status, response_body=err,
                               **kwargs)

    def _process_reply(self, reply, notification, router_data, ttl):
        """Process FCM send reply"""
        # acks:
        #  for reg_id, msg_id in reply.success.items():
        # updates
        result = reply.get('results', [{}])[0]
        if reply.get('canonical_ids'):
            old_id = router_data['token']
            new_id = result.get('registration_id')
            self.log.info("FCM id changed : {old} => {new}",
                          old=old_id, new=new_id)
            self.metrics.increment("updates.client.bridge.fcm.failed.rereg",
                                   self._base_tags)
            return RouterResponse(status_code=503,
                                  response_body="Please try request again.",
                                  router_data=dict(token=new_id))
        if reply.get('failure'):
            self.metrics.increment("updates.client.bridge.fcm.failed",
                                   self._base_tags)
            reason = result.get('error', "Unreported")
            err = self.reasonTable.get(reason)
            if err.get("crit", False):
                self.log.critical(
                    err['msg'],
                    nlen=len(notification.data),
                    regid=router_data["token"],
                    senderid=self.senderID,
                    ttl=notification.ttl,
                )
                raise RouterException("FCM failure to deliver",
                                      status_code=err['err'],
                                      response_body="Please try request "
                                                    "later.",
                                      log_exception=False)
            creds = router_data["creds"]
            self.log.info("{msg} : {info}",
                          msg=err['msg'],
                          info={"senderid": creds.get('registration_id'),
                                "reason": reason})
            return RouterResponse(
                status_code=err['err'],
                errno=err['errno'],
                response_body=err['msg'],
                router_data={},
            )
        self.metrics.increment("updates.client.bridge.fcm.succeeded",
                               self._base_tags)
        location = "%s/m/%s" % (self.ap_settings.endpoint_url,
                                notification.version)
        return RouterResponse(status_code=201, response_body="",
                              headers={"TTL": ttl,
                                       "Location": location},
                              logged_status=200)
