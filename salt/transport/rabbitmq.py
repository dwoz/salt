"""
Rabbitmq transport classes
"""
import hashlib
import logging
import signal
import sys
import threading
import time
from typing import Any, Callable

# pylint: disable=3rd-party-module-not-gated
import pika
import salt.auth
import salt.crypt
import salt.ext.tornado
import salt.ext.tornado.concurrent
import salt.ext.tornado.gen
import salt.ext.tornado.ioloop
import salt.log.setup
import salt.payload
import salt.transport.client
import salt.transport.server
import salt.utils.event
import salt.utils.files
import salt.utils.minions
import salt.utils.process
import salt.utils.stringutils
import salt.utils.verify
import salt.utils.versions
import salt.utils.zeromq
from pika import BasicProperties, SelectConnection
from pika.exceptions import ChannelClosedByBroker, ConnectionClosedByBroker
from pika.exchange_type import ExchangeType
from pika.spec import PERSISTENT_DELIVERY_MODE
from salt.exceptions import SaltReqTimeoutError
from salt.ext import tornado

# pylint: enable=3rd-party-module-not-gated

log = logging.getLogger(__name__)


class RMQConnectionWrapperBase:
    def __init__(self, opts, **kwargs):
        self.log = (
            kwargs["log"]
            if "log" in kwargs
            else logging.getLogger(self.__class__.__name__)
        )

        self._opts = opts
        self._validate_set_broker_topology(self._opts, **kwargs)

        self._connection = None
        self._channel = None
        self._closing = False

    def _validate_set_broker_topology(self, opts, **kwargs):
        """
        Validate broker topology and set private variables. For example:
            {
                "transport: rabbitmq",
                "transport_rabbitmq_url": "amqp://salt:salt@localhost",
                "transport_rabbitmq_create_topology_ondemand": "True",
                "transport_rabbitmq_publisher_exchange_name":" "exchange_to_publish_messages",
                "transport_rabbitmq_consumer_exchange_name": "exchange_to_bind_queue_to_receive_messages_from",
                "transport_rabbitmq_consumer_queue_name": "queue_to_consume_messages_from",

                "transport_rabbitmq_consumer_queue_declare_arguments": {
                    "x-expires": 600000,
                    "x-max-length": 10000,
                    "x-queue-type": "quorum",
                    "x-queue-mode": "lazy",
                    "x-message-ttl": 259200000,
                },
                "transport_rabbitmq_publisher_exchange_declare_arguments": ""
                "transport_rabbitmq_consumer_exchange_declare_arguments": ""
            }
        """

        # rmq broker url (encodes credentials, address, vhost). See https://www.rabbitmq.com/uri-spec.html
        self._url = (
            opts["transport_rabbitmq_url"]
            if "transport_rabbitmq_url" in opts
            else "amqp://salt:salt@localhost"
        )
        if not self._url:
            raise ValueError("RabbitMQ URL must be set")

        # optionally create the RMQ topology if instructed
        # some use cases require topology creation out-of-band with permissions
        # rmq consumer queue arguments
        create_topology_key = "transport_rabbitmq_create_topology_ondemand"
        self._create_topology_ondemand = (
            kwargs.get(create_topology_key, False)
            if create_topology_key in kwargs
            else opts.get(create_topology_key, False)
        )

        # publisher exchange name
        publisher_exchange_name_key = "transport_rabbitmq_publisher_exchange_name"
        if (
            publisher_exchange_name_key not in opts
            and publisher_exchange_name_key not in kwargs
        ):
            raise KeyError(
                "Missing configuration key {!r}".format(publisher_exchange_name_key)
            )
        self._publisher_exchange_name = (
            kwargs.get(publisher_exchange_name_key)
            if publisher_exchange_name_key in kwargs
            else opts[publisher_exchange_name_key]
        )

        # consumer exchange name
        consumer_exchange_name_key = "transport_rabbitmq_consumer_exchange_name"
        if (
            consumer_exchange_name_key not in opts
            and consumer_exchange_name_key not in kwargs
        ):
            raise KeyError(
                "Missing configuration key {!r}".format(consumer_exchange_name_key)
            )
        self._consumer_exchange_name = (
            kwargs.get(consumer_exchange_name_key)
            if consumer_exchange_name_key in kwargs
            else opts[consumer_exchange_name_key]
        )

        # consumer queue name
        consumer_queue_name_key = "transport_rabbitmq_consumer_queue_name"
        if (
            consumer_queue_name_key not in opts
            and consumer_queue_name_key not in kwargs
        ):
            raise KeyError(
                "Missing configuration key {!r}".format(consumer_queue_name_key)
            )
        self._consumer_queue_name = (
            kwargs.get(consumer_queue_name_key)
            if consumer_queue_name_key in kwargs
            else opts[consumer_queue_name_key]
        )

        # rmq consumer exchange arguments when declaring exchanges
        consumer_exchange_declare_arguments_key = (
            "transport_rabbitmq_consumer_exchange_declare_arguments"
        )
        self._consumer_exchange_declare_arguments = (
            kwargs.get(consumer_exchange_declare_arguments_key)
            if consumer_exchange_declare_arguments_key in kwargs
            else opts.get(consumer_exchange_declare_arguments_key, None)
        )

        # rmq consumer queue arguments when declaring queues
        consumer_queue_declare_arguments_key = (
            "transport_rabbitmq_consumer_queue_declare_arguments"
        )
        self._consumer_queue_declare_arguments = (
            kwargs.get(consumer_queue_declare_arguments_key)
            if consumer_queue_declare_arguments_key in kwargs
            else opts.get(consumer_queue_declare_arguments_key, None)
        )

        # rmq publisher exchange arguments when declaring exchanges
        publisher_exchange_declare_arguments_key = (
            "transport_rabbitmq_publisher_exchange_declare_arguments"
        )
        self._publisher_exchange_declare_arguments = (
            kwargs.get(publisher_exchange_declare_arguments_key)
            if publisher_exchange_declare_arguments_key in kwargs
            else opts.get(publisher_exchange_declare_arguments_key, None)
        )

    @property
    def queue_name(self):
        return self._consumer_queue_name

    def close(self):
        if not self._closing:
            try:
                if self._channel and self._channel.is_open:
                    self._channel.close()
                    self._channel = None
            except pika.exceptions.ChannelWrongStateError:
                pass

            try:
                if self._connection and self._connection.is_open:
                    self._connection.close()
                    self._connection = None
            except pika.exceptions.ConnectionClosedByBroker:
                pass
            self._closing = True
        else:
            self.log.debug("Already closing. Do nothing")


class RMQBlockingConnectionWrapper(RMQConnectionWrapperBase):

    """
    RMQConnection wrapper implemented that wraps a BlockingConnection.
    Declares and binds a queue to a fanout exchange for publishing messages.
    Caches connection and channel for reuse.
    Not thread safe.
    """

    def __init__(self, opts, **kwargs):
        super().__init__(opts, **kwargs)

        self._connect(self._url)

        if self._create_topology_ondemand:
            try:
                self._create_topology()
            except:
                self.log.exception("Exception when creating RMQ topology.")
                raise
        else:
            log.info("Skipping rmq topology creation.")

    def _connect(self, url):
        self.log.info("Connecting to amqp broker identified by URL [%s]", self._url)
        self._connection = pika.BlockingConnection(pika.URLParameters(url))
        self._channel = self._connection.channel()

    def _create_topology(self):

        if self._publisher_exchange_name:
            ret = self._channel.exchange_declare(
                exchange=self._publisher_exchange_name,
                exchange_type=ExchangeType.fanout,
                durable=True,
                auto_delete=True,
                arguments=self._publisher_exchange_declare_arguments,
            )
            self.log.info(
                "Declared publisher exchange: %s",
                self._publisher_exchange_name,
            )
        else:
            self.log.info("Skipping publisher exchange declaration")

        if self._consumer_exchange_name:
            ret = self._channel.exchange_declare(
                exchange=self._consumer_exchange_name,
                exchange_type=ExchangeType.fanout,
                durable=True,
                auto_delete=True,
                arguments=self._consumer_exchange_declare_arguments,
            )
            self.log.info(
                "Declared consumer exchange: %s",
                self._consumer_exchange_name,
            )
        else:
            self.log.info("Skipping consumer exchange declaration")

        if self._consumer_queue_name:
            ret = self._channel.queue_declare(
                self._consumer_queue_name,
                durable=True,
                arguments=self._consumer_queue_declare_arguments,
            )
            self.log.info(
                "declared queue: %s",
                self._consumer_queue_name,
            )

            self.log.info(
                "Binding queue [%s] to exchange [%s]",
                self._consumer_queue_name,
                self._consumer_exchange_name,
            )
            ret = self._channel.queue_bind(
                self._consumer_queue_name,
                self._consumer_exchange_name,
                routing_key=self._consumer_queue_name,
            )
            self.log.info(
                "Bound queue [%s] to exchange [%s]",
                self._consumer_queue_name,
                self._consumer_exchange_name,
            )
        else:
            self.log.info("Skipping consumer queue binding to consumer exchange")

    def publish(
        self,
        payload,
        exchange_name=None,
        routing_key="",  # must be a string
        reply_queue_name=None,
    ):
        """
        Publishes ``payload`` to the specified ``exchange_name`` (via direct exchange or via fanout/broadcast) with
        ``routing_key``, passes along optional name of the reply queue in message metadata. Non-blocking.
        Recover from connection failure (with a retry).

        :param reply_queue_name: optional reply queue name
        :param payload: message body
        :param exchange_name: exchange name
        :param routing_key: optional name of the routing key, exchange specific
        (it will be deleted when the last consumer disappears)
        :return:
        """
        while (
            not self._closing
        ):  # TODO: limit number of retries and use some retry decorator
            try:
                self._publish(
                    payload,
                    exchange_name=exchange_name or self._publisher_exchange_name,
                    routing_key=routing_key,
                    reply_queue_name=reply_queue_name,
                )

                break

            except pika.exceptions.ConnectionClosedByBroker:
                # Connection may have been terminated cleanly by the broker, e.g.
                # as a result of "rabbitmqtl" cli call. Attempt to reconnect anyway.
                self.log.exception("Connection exception when publishing.")
                self.log.info("Attempting to re-establish RMQ connection.")
                self._connect(self._url)
            except pika.exceptions.ChannelWrongStateError:
                # Note: RabbitMQ uses heartbeats to detect and close "dead" connections and to prevent network devices
                # (firewalls etc.) from terminating "idle" connections.
                # From version 3.5.5 on, the default timeout is set to 60 seconds
                self.log.exception("Channel exception when publishing.")
                self.log.info("Attempting to re-establish RMQ connection.")
                self._connect(self._url)

    def _publish(
        self,
        payload,
        exchange_name=None,
        routing_key="",  # must be a string
        reply_queue_name=None,
    ):
        """
        Publishes ``payload`` to the specified ``exchange_name`` (via direct exchange or via fanout/broadcast),
        passes along optional name of the reply queue in message metadata. Non-blocking.
        Alternatively, broadcasts the ``payload`` to all bound queues (via fanout exchange).

        :param payload: message body
        :param exchange_name: exchange name
        :param routing_key: and exchange-specific routing key
        :param reply_queue_name: optional name of the reply queue
        :return:
        """
        properties = pika.BasicProperties()
        properties.reply_to = reply_queue_name if reply_queue_name else None
        properties.app_id = str(threading.get_ident())  # use this for tracing
        # enable persistent message delivery
        # see https://www.rabbitmq.com/confirms.html#publisher-confirms and
        # https://kousiknath.medium.com/dabbling-around-rabbit-mq-persistence-durability-message-routing-f4efc696098c
        properties.delivery_mode = PERSISTENT_DELIVERY_MODE

        self.log.info(
            "Sending payload to exchange [%s]: %s. Payload properties: %s",
            exchange_name,
            payload,
            properties,
        )

        try:
            self._channel.basic_publish(
                exchange=exchange_name,
                routing_key=routing_key,
                body=payload,
                properties=properties,  # added reply queue to the properties
                mandatory=True,
            )
            self.log.info(
                "Sent payload to exchange [%s] with routing_key [%s]: [%s]",
                exchange_name,
                routing_key,
                payload,
            )
        except:
            self.log.exception(
                "Error publishing to exchange [%s] with routing_key [%s]",
                exchange_name,
                routing_key,
            )
            raise

    def publish_reply(self, payload, properties: BasicProperties):
        """
        Publishes reply ``payload`` to the reply queue. Non-blocking.
        :param payload: message body
        :param properties: payload properties/metadata
        :return:
        """
        reply_to = properties.reply_to

        if not reply_to:
            raise ValueError("properties.reply_to must be set")

        self.log.info(
            "Sending reply payload with reply_to [%s]: %s. Payload properties: %s",
            reply_to,
            payload,
            properties,
        )

        self.publish(
            payload,
            exchange_name="",  # ExchangeType.direct,  # use the default exchange for replies
            routing_key=reply_to,
        )

    def consume(self, queue_name=None, timeout=60):
        """
        A non-blocking consume takes the next message off the queue.
        :param queue_name:
        :param timeout:
        :return:
        """
        self.log.info("Consuming payload on queue [%s]", queue_name)

        queue_name = queue_name or self._consumer_queue_name
        (method, properties, body) = next(
            self._channel.consume(
                queue=queue_name, inactivity_timeout=timeout, auto_ack=True
            )
        )
        return body

    def register_reply_callback(self, callback, reply_queue_name=None):
        """
        Registers RPC reply callback on the reply queue
        :param callback:
        :param reply_queue_name:
        :return:
        """

        def _callback(ch, method, properties, body):
            self.log.info(
                "Received reply on queue [%s]: %s. Reply payload properties: %s",
                reply_queue_name,
                body,
                properties,
            )
            callback(body)
            self.log.info(
                "Processed callback for reply on queue [%s]", reply_queue_name
            )

            # Stop consuming so that auto_delete queue will be deleted
            self._channel.stop_consuming()
            self.log.info("Done consuming reply on queue [%s]", reply_queue_name)

        self.log.info("Starting basic_consume reply on queue [%s]", reply_queue_name)

        consumer_tag = self._channel.basic_consume(
            queue=reply_queue_name, on_message_callback=_callback, auto_ack=True
        )

        self.log.info("Started basic_consume reply on queue [%s]", reply_queue_name)

    def start_consuming(self):
        """
        Blocks and dispatches callbacks configured by ``self._channel.basic_consume()``
        :return:
        """
        # a blocking call until self._channel.stop_consuming() is called
        self._channel.start_consuming()


class RMQNonBlockingConnectionWrapper(RMQConnectionWrapperBase):
    """
    Async RMQConnection wrapper implemented in a Continuation-Passing style. Reuses a custom io_loop.
    Declares and binds a queue to a fanout exchange for publishing messages.
    Caches connection and channel for reuse.
    Not thread safe.
    Implements some event-based connection recovery
    """

    def __init__(self, opts, io_loop=None, **kwargs):
        super().__init__(opts, **kwargs)

        self._io_loop = io_loop or tornado.ioloop.IOLoop()

        self._channel = None
        self._message_callback = None
        self._connection_future = (
            None  # a future that is complete when connection is ready or in error
        )

    @salt.ext.tornado.gen.coroutine
    def connect(self, message_callback=None):
        """
        Do not reconnect if we are already connected
        :return:
        """
        if (
            self._connection
            and self._connection.is_open
            and self._channel
            and self._channel.is_open
        ):
            self.log.info("Already connected")
            return

        if message_callback:
            self._message_callback = message_callback

        self._connection_future = salt.ext.tornado.concurrent.Future()
        res = yield self._connect()
        return res

    @salt.ext.tornado.gen.coroutine
    def _connect(self):
        """

        :return:
        """
        self.log.info("Connecting to amqp broker identified by URL [%s]", self._url)

        self._connection = SelectConnection(
            parameters=pika.URLParameters(self._url),
            on_open_callback=self._on_connection_open,
            on_open_error_callback=self._on_connection_error,
            custom_ioloop=self._io_loop,
        )

        res = yield self._connection_future
        return res

    def register_message_callback(self, message_callback=None):
        """

        :param message_callback:
        :return:
        """
        self._message_callback = message_callback

    def _reconnect(self):
        """Will be invoked by the IOLoop timer if the connection is
        closed. See the on_connection_closed method.

        Note: RabbitMQ uses heartbeats to detect and close "dead" connections and to prevent network devices
        (firewalls etc.) from terminating "idle" connections.
        From version 3.5.5 on, the default timeout is set to 60 seconds

        """
        if not self._closing:
            # Create a new connection
            self.log.info("Reconnecting...")
            # sleep for a bit so that if for some reason we get into a reconnect loop we won't kill the system
            time.sleep(2)
            self._connection = None
            self._channel = None
            self.connect(message_callback=self._message_callback)

    def _on_connection_open(self, connection):
        """
        Invoked by pika when connection is opened successfully
        :param connection:
        :return:
        """
        self._connection = connection
        connection.add_on_close_callback(self._on_connection_closed)

        self._channel = connection.channel(on_open_callback=self._on_channel_open)
        self._channel.add_on_close_callback(self._on_channel_closed)

    def _on_connection_error(self, connection, exception):
        """
        Invoked by pika on connection error
        :param connection:
        :param exception:
        :return:
        """
        log.error("Failed to connect", exc_info=True)

    def _on_connection_closed(self, connection, reason):
        """This method is invoked by pika when the connection to RabbitMQ is
        closed unexpectedly. Since it is unexpected, we will reconnect to
        RabbitMQ if it disconnects.

        :param pika.connection.Connection connection: The closed connection obj
        :param Exception reason: exception representing reason for loss of
            connection.

        """
        self.log.debug("Connection closed for reason [%s]", reason)
        if isinstance(reason, ChannelClosedByBroker) or isinstance(
            reason, ConnectionClosedByBroker
        ):
            if reason.reply_code == 404:
                self.log.debug(
                    "Not recovering from 404. Make sure RMQ topology exists."
                )
                raise reason
            else:
                self._reconnect()

    def _on_channel_closed(self, channel, reason):
        self.log.debug("Channel closed for reason [%s]", reason)
        if isinstance(reason, ChannelClosedByBroker):
            if reason.reply_code == 404:
                self.log.warning(
                    "Not recovering from 404. Make sure RMQ topology exists."
                )
                raise reason
            else:
                self._reconnect()
        else:
            self.log.debug(
                "Not attempting to recover. Channel likely closed for legitimate reasons."
            )

    def _on_channel_open(self, channel):
        """
        Invoked by pika when channel is opened successfully
        :param channel:
        :return:
        """
        self.log.info("Channel opened: %s", channel)

        self._channel = channel
        if self._create_topology_ondemand:
            channel.exchange_declare(
                exchange=self._publisher_exchange_name,
                exchange_type=ExchangeType.fanout,
                durable=True,
                auto_delete=True,
                callback=self._on_publisher_exchange_declared,
            )
        else:
            self.log.info("Skipping rmq topology creation.")
            if self._message_callback:
                self.start_consuming(self._message_callback, self._connection_future)
            else:
                self._connection_done(self._connection_future)

    def _connection_done(self, future):
        if not future.done():
            future.set_result(self._channel)

    def _on_publisher_exchange_declared(self, method):
        """Invoked by pika when RabbitMQ has finished the Exchange.Declare RPC
        command.
        """
        self.log.info("Publisher exchange declared: %s", self._publisher_exchange_name)
        if not self._channel:
            raise ValueError("_channel must be set")

        if self._consumer_exchange_name:
            self._channel.exchange_declare(
                exchange=self._consumer_exchange_name,
                exchange_type=ExchangeType.fanout,
                durable=True,
                auto_delete=True,
                callback=self._on_consumer_exchange_declared,
            )
        else:
            log.info(
                "No consumer exchange configured. Skipping consumer exchange declaration"
            )
            self._connection_done(self._connection_future)

    def _on_consumer_exchange_declared(self, method):
        """Invoked by pika when RabbitMQ has finished the Exchange.Declare RPC
        command.
        """
        self.log.info("Consumer exchange declared: %s", self._consumer_exchange_name)
        if not self._channel:
            raise ValueError("_channel must be set")

        if self.queue_name:
            self._channel.queue_declare(
                queue=self._consumer_queue_name,
                durable=True,
                arguments=self._consumer_queue_declare_arguments,
                callback=self._on_consumer_queue_declared,
            )
        else:
            log.info(
                "No consumer queue configured. Skipping basic_consume on queue %s",
                self.queue_name,
            )
            self._connection_done(self._connection_future)

    def _on_consumer_queue_declared(self, method):
        """
        Invoked by pika when queue is declared successfully
        :param method:
        :return:
        """
        self.log.info("Consumer queue declared: %s", method.method.queue)
        if not self._channel:
            raise ValueError("_channel must be set")

        self._channel.queue_bind(
            method.method.queue,
            self._consumer_exchange_name,
            routing_key=method.method.queue,
            callback=self._on_consumer_queue_bound,
        )

    def _on_consumer_queue_bound(self, method):
        """
        Invoked by pika when queue bound successfully. Set up consumer message callback as well.
        :param method:
        :return:
        """

        self.log.info("Queue bound [%s]", self.queue_name)
        if self._message_callback:
            self.start_consuming(self._message_callback, self._connection_future)
        else:
            log.info(
                "No message callback configured. Skipping basic_consume on queue %s",
                self.queue_name,
            )
            self._connection_done(self._connection_future)

    @salt.ext.tornado.gen.coroutine
    def start_consuming(
        self, callback: Callable[[Any, BasicProperties], None], future=None
    ):
        """
        :param future:
        :param callback:
        :return:
        """
        future = future or salt.ext.tornado.concurrent.Future()

        def _callback_consumer_registered(method):
            if not future.done():
                future.set_result(self._channel)

        def _on_message_callback_wrapper(channel, method, properties, payload):
            self.log.info(
                "Received message on queue [%s]: %s. Payload properties: %s",
                self.queue_name,
                payload,
                properties,
            )

            if callback:
                callback(payload, message_properties=properties)
                log.info(
                    "Processed callback for message on queue [%s]", self.queue_name
                )

        if not self._channel:
            raise ValueError("_channel must be set")

        self._channel.basic_consume(
            self.queue_name,
            callback=_callback_consumer_registered,
            on_message_callback=_on_message_callback_wrapper,
            auto_ack=True,
        )

        self.log.info("Starting basic_consume on queue [%s]", self.queue_name)

        yield future

    @salt.ext.tornado.gen.coroutine
    def consume_reply(self, callback, reply_queue_name=None):
        """
        Registers RPC reply callback on the designated reply queue
        :param callback:
        :param reply_queue_name:
        :return:
        """
        future = salt.ext.tornado.concurrent.Future()

        def _callback_consumer_registered(method):
            future.set_result(method.method.consumer_tag)

        def _on_message_callback(ch, method, properties, body):
            self.log.info(
                "Received reply on queue [%s]: %s. Reply payload properties: %s",
                reply_queue_name,
                body,
                properties,
            )
            callback(body, properties.correlation_id)
            self.log.info(
                "Processed callback for reply on queue [%s]", reply_queue_name
            )

        self.log.info("Starting basic_consume reply on queue [%s]", reply_queue_name)

        if not self._channel:
            raise ValueError("_channel must be set")

        consumer_tag = "rmq_direct_reply_consumer"  # an arbitrarily chosen consumer tag
        try:
            consumer_tag = self._channel.basic_consume(
                consumer_tag=consumer_tag,
                queue=reply_queue_name,
                on_message_callback=_on_message_callback,
                auto_ack=True,
                callback=_callback_consumer_registered,
            )
        except pika.exceptions.DuplicateConsumerTag:
            # this could happen when retrying to send the request multiple times and the retry loop will end up here
            # see ```timeout_message``` for reference
            self.log.warning(
                "Ignoring attempt to set up a second consumer with consumer tag [%s]",
                consumer_tag,
            )
            future.set_result(consumer_tag)

        self.log.info(
            "Started basic_consume reply on queue [%s] with consumer tag [%s]",
            reply_queue_name,
            consumer_tag,
        )

        yield future

    @salt.ext.tornado.gen.coroutine
    def publish(
        self,
        payload,
        exchange_name=None,
        routing_key="",  # must be a string
        reply_queue_name=None,
        correlation_id=None,
    ):
        """
        Publishes ``payload`` to the specified exchange ``exchange_name`` with the routing key ``routing_key``
        (via direct exchange or via fanout/broadcast),
        passes along optional name of the reply queue in message metadata. Non-blocking.
        Alternatively, broadcasts the ``payload`` to all bound queues (via fanout exchange).

        :param correlation_id: optional message correlation id (used in RPC request/response pattern)
        :param payload: message body
        :param exchange_name: exchange name
        :param routing_key: and exchange-specific routing key
        :param reply_queue_name: optional name of the reply queue
        :return:
        """
        properties = pika.BasicProperties()
        properties.reply_to = reply_queue_name if reply_queue_name else None
        properties.app_id = str(threading.get_ident())  # use this for tracing
        # enable persistent message delivery
        # see https://www.rabbitmq.com/confirms.html#publisher-confirms and
        # https://kousiknath.medium.com/dabbling-around-rabbit-mq-persistence-durability-message-routing-f4efc696098c
        properties.delivery_mode = PERSISTENT_DELIVERY_MODE
        # This property helps relate request/response when using amq.rabbitmq.reply-to pattern
        properties.correlation_id = correlation_id or str(hash(payload))

        # Note: exchange name that is an empty string ("") has meaning -- it is the name of the "direct exchange"
        exchange_name = (
            exchange_name
            if exchange_name is not None
            else self._publisher_exchange_name
        )
        self.log.info(
            "Sending payload to exchange [%s]: %s. Payload properties: %s",
            exchange_name,
            payload,
            properties,
        )

        try:
            self._channel.basic_publish(
                exchange=exchange_name,
                routing_key=routing_key,
                body=payload,
                properties=properties,
                mandatory=True,
            )
            self.log.info(
                "Sent payload to exchange [%s] with routing_key [%s]: [%s]",
                exchange_name,
                routing_key,
                payload,
            )
        except:
            self.log.exception(
                "Error publishing to exchange [%s] with routing_key [%s]",
                exchange_name,
                routing_key,
            )
            raise

    @salt.ext.tornado.gen.coroutine
    def publish_reply(self, payload, **optional_transport_args):
        """
        Publishes reply ``payload`` routing it to the reply queue. Non-blocking.
        :param payload: message body
        :param optional_transport_args: payload properties/metadata
        :return:
        """

        message_properties = None
        if optional_transport_args:
            message_properties = optional_transport_args.get("message_properties", None)
            if not isinstance(message_properties, BasicProperties):
                raise TypeError(
                    "message_properties must be of type {!r} instead of {!r}".format(
                        type(BasicProperties), type(message_properties)
                    )
                )

        routing_key = message_properties.reply_to

        if not routing_key:
            raise ValueError("properties.reply_to must be set")

        self.log.info(
            "Sending reply payload to direct exchange with routing key [%s]: %s. Payload properties: %s",
            routing_key,
            payload,
            message_properties,
        )

        # publish reply on a queue that will be deleted after consumer cancels or disconnects
        # do not broadcast replies
        yield self.publish(
            payload,
            exchange_name="",  # use the special default/direct exchange for replies with the name that is empty string
            routing_key=routing_key,
            correlation_id=message_properties.correlation_id,
        )


class RabbitMQRequestClient(salt.transport.base.RequestClient):
    ttype = "rabbitmq"

    def __init__(self, opts, io_loop, **kwargs):
        super().__init__(opts, io_loop, **kwargs)
        self.opts = opts
        self.message_client = AsyncReqMessageClient(
            self.opts,
            # self.master_uri,
            io_loop=io_loop,
        )

    @salt.ext.tornado.gen.coroutine
    def send(self, load, tries=3, timeout=60):
        ret = yield self.message_client.send(load, timeout, tries)
        raise salt.ext.tornado.gen.Return(ret)

    def close(self):
        self.message_client.close()


class RabbitMQPubClient(salt.transport.base.PublishClient):
    """
    A transport channel backed by RabbitMQ for a Salt Publisher to use to
    publish commands to connected minions.
    Typically, this class is instantiated by a minion
    """

    def __init__(self, opts, io_loop, **kwargs):
        super().__init__(opts, io_loop, **kwargs)
        self.log = logging.getLogger(self.__class__.__name__)
        self.opts = opts
        self.ttype = "rabbitmq"
        self.io_loop = io_loop
        if not self.io_loop:
            raise ValueError("self.io_loop must be set")

        self._closing = False

        self.hexid = hashlib.sha1(
            salt.utils.stringutils.to_bytes(self.opts["id"])
        ).hexdigest()
        self.auth = salt.crypt.AsyncAuth(self.opts, io_loop=self.io_loop)
        self.serial = salt.payload.Serial(self.opts)
        self._rmq_non_blocking_connection_wrapper = RMQNonBlockingConnectionWrapper(
            self.opts,
            io_loop=self.io_loop,
            log=self.log,
        )

    def close(self):
        if self._closing is True:
            return
        self._rmq_non_blocking_connection_wrapper.close()
        self._closing = True

    # pylint: disable=no-dunder-del
    def __del__(self):
        self.close()

    # pylint: enable=no-dunder-del
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # TODO: this is the time to see if we are connected, maybe use the req channel to guess?
    @salt.ext.tornado.gen.coroutine
    def connect(
        self, publish_port=None, connect_callback=None, disconnect_callback=None
    ):
        """
        Connects minion to master.
        :return:
        """
        # connect() deserves its own method wrapped in a coroutine called by the upstream layer when appropriate
        yield self._rmq_non_blocking_connection_wrapper.connect()

        if not self.auth.authenticated:
            yield self.auth.authenticate()

        log.info(
            "Minion already authenticated with master. Nothing else to do for broker-based transport."
        )

    @salt.ext.tornado.gen.coroutine
    def _decode_messages(self, messages):
        """
        Take the rmq messages, decrypt/decode them into a payload

        :param list messages: A list of messages to be decoded
        """
        messages = [
            messages
        ]  # TODO: FIXME - figure out why this does not match zeromq payload packing
        messages_len = len(messages)
        # if it was one message, then its old style
        if messages_len == 1:
            if isinstance(messages[0], dict):
                return messages[0]
            else:
                payload = salt.payload.loads(messages[0])
                payload = (
                    salt.payload.loads(payload["payload"])
                    if "payload" in payload
                    else payload
                )
        # 2 includes a header which says who should do it
        elif messages_len == 2:
            message_target = salt.utils.stringutils.to_str(messages[0])
            if (
                self.opts.get("__role") != "syndic"
                and message_target not in ("broadcast", self.hexid)
            ) or (
                self.opts.get("__role") == "syndic"
                and message_target not in ("broadcast", "syndic")
            ):
                log.info("Publish received for not this minion: %s", message_target)
                raise salt.ext.tornado.gen.Return(None)
            payload = salt.payload.loads(messages[1])
        else:
            raise Exception(
                (
                    "Invalid number of messages ({}) in rabbitmq pub"
                    "message from master"
                ).format(len(messages_len))
            )
        # Yield control back to the caller. When the payload has been decoded, assign
        # the decoded payload to 'ret' and resume operation
        # ret = yield self._decode_payload(payload)
        raise salt.ext.tornado.gen.Return(payload)

    def on_recv(self, callback):
        """
        Register an on_recv callback
        """
        if callback:

            @salt.ext.tornado.gen.coroutine
            def wrap_callback(messages, **kwargs):
                payload = yield self._decode_messages(messages)
                if payload is not None:
                    callback(payload)

            self._rmq_non_blocking_connection_wrapper.start_consuming(wrap_callback)


class RabbitMQReqServer(salt.transport.base.RequestServer):
    """
    Encapsulate synchronous operations for a request channel
    Typically, this class is instantiated by a master
    """

    def __init__(self, opts):
        super().__init__(opts)
        self.log = logging.getLogger(self.__class__.__name__)
        self.opts = opts
        self._closing = False
        self._monitor = None
        self._w_monitor = None

    def close(self):
        """
        Cleanly shutdown the router socket
        """
        if self._closing:
            return

    def post_fork(self, message_handler, io_loop):
        """
        After forking we need to set up handlers to listen to the
        router

        :param func message_handler: A function to called to handle incoming payloads as
                                     they are picked up off the wire
        :param IOLoop io_loop: An instance of a Tornado IOLoop, to handle event scheduling
        """

        if not io_loop:
            raise ValueError("io_loop must be set")

        self.payload_handler = message_handler
        self._rmq_nonblocking_connection_wrapper = RMQNonBlockingConnectionWrapper(
            self.opts, io_loop=io_loop, log=self.log
        )

        # PR - FIXME. Consider moving the connect() call into a coroutine to that we can yield it
        self._rmq_nonblocking_connection_wrapper.connect(
            message_callback=self.handle_message
        )

    @salt.ext.tornado.gen.coroutine
    def handle_message(
        self, payload, **optional_transport_args
    ):  # message_properties: pika.BasicProperties
        payload = self.decode_payload(payload)
        reply = yield self.payload_handler(payload, **optional_transport_args)
        yield self._rmq_nonblocking_connection_wrapper.publish_reply(
            self.encode_payload(reply), **optional_transport_args
        )

    def encode_payload(self, payload):
        return salt.payload.dumps(payload)

    def decode_payload(self, payload):
        payload = salt.payload.loads(payload)
        return payload

    def __setup_signals(self):
        signal.signal(signal.SIGINT, self._handle_signals)
        signal.signal(signal.SIGTERM, self._handle_signals)

    def _handle_signals(self, signum, sigframe):
        msg = "{} received a ".format(self.__class__.__name__)
        if signum == signal.SIGINT:
            msg += "SIGINT"
        elif signum == signal.SIGTERM:
            msg += "SIGTERM"
        msg += ". Exiting"
        log.info(msg)
        self.close()
        sys.exit(salt.defaults.exitcodes.EX_OK)


class RabbitMQPublishServer(salt.transport.base.PublishServer):
    """
    Encapsulate synchronous operations for a publisher channel.
    Typically, this class is instantiated by a master
    """

    def __init__(self, opts):
        super().__init__()
        self.log = logging.getLogger(self.__class__.__name__)
        self.opts = opts
        self.serial = salt.payload.Serial(self.opts)  # TODO: in init?
        self.ckminions = salt.utils.minions.CkMinions(self.opts)

        self._rmq_blocking_connection_wrapper = RMQBlockingConnectionWrapper(
            opts,
            log=self.log,
        )

    def connect(self):
        return salt.ext.tornado.gen.sleep(5)  # TODO: why is this here?

    def pre_fork(self, process_manager, kwargs=None):
        """
        Do anything necessary pre-fork. Since this is on the master side this will
        primarily be used to create IPC channels and create our daemon process to
        do the actual publishing

        :param func process_manager: A ProcessManager, from salt.utils.process.ProcessManager
        """

    def pub_connect(self):
        """
        Do nothing, assuming RMQ broker is running
        """

    def publish(self, payload, **optional_transport_args):
        """
        Publish "load" to minions. This sends the load to the RMQ broker
        process which does the actual sending to minions.

        :param dict payload: A load to be sent across the wire to minions
        """

        payload_serialized = salt.payload.dumps(payload)

        log.info(
            "Sending payload to rabbitmq publish daemon. jid=%s size=%d",
            payload.get("jid", None),
            len(payload_serialized),
        )

        message_properties = None
        if optional_transport_args:
            message_properties = optional_transport_args.get("message_properties", None)
            if not isinstance(message_properties, BasicProperties):
                raise TypeError(
                    "message_properties must be of type {!r} instead of {!r}".format(
                        type(BasicProperties), type(message_properties)
                    )
                )

        # send
        self._rmq_blocking_connection_wrapper.publish(
            payload_serialized,
            reply_queue_name=message_properties.reply_to
            if message_properties
            else None,
        )
        log.info("Sent payload to rabbitmq publish daemon.")

    @property
    def topic_support(self):
        # we may support this eventually
        return False

    def close(self):
        self._rmq_blocking_connection_wrapper.close()

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        self.close()


# TODO: unit tests!
class AsyncReqMessageClient:
    """
    This class gives a future-based
    interface to sending and receiving messages. This works around the primary
    limitation of serialized send/recv on the underlying socket by queueing the
    message sends in this class. In the future if we decide to attempt to multiplex
    we can manage a pool of REQ/REP sockets-- but for now we'll just do them in serial

    """

    def __init__(self, opts, io_loop=None):
        """
        Create an asynchronous message client

        :param dict opts: The salt opts dictionary
        :param IOLoop io_loop: A Tornado IOLoop event scheduler [tornado.ioloop.IOLoop]
        """

        self.log = logging.getLogger(self.__class__.__name__)
        self.io_loop = io_loop or salt.ext.tornado.ioloop.IOLoop()
        self.opts = opts

        if (
            self.opts.get("__role") == "master"
        ):  # TODO: fix this so that this check is done upstream
            # local client uses master config file but acts as a client to the master
            # swap the exchange to that we can reach the master
            self._rmq_non_blocking_connection_wrapper = RMQNonBlockingConnectionWrapper(
                self.opts,
                io_loop=self.io_loop,
                log=self.log,
                transport_rabbitmq_publisher_exchange_name=self.opts[
                    "transport_rabbitmq_consumer_exchange_name"
                ],
                transport_rabbitmq_consumer_exchange_name=None,
                transport_rabbitmq_consumer_queue_name=None,
            )
        else:
            self._rmq_non_blocking_connection_wrapper = RMQNonBlockingConnectionWrapper(
                self.opts, io_loop=self.io_loop, log=self.log
            )

        self.serial = salt.payload.Serial(self.opts)

        self.send_queue = []
        # mapping of str(hash(message)) -> future
        self.send_future_map = {}

        self.send_timeout_map = {}  # message -> timeout
        self._closing = False

    # TODO: timeout all in-flight sessions, or error
    def close(self):
        try:
            if self._closing:
                return
            self._rmq_non_blocking_connection_wrapper.close()
        except AttributeError:
            # We must have been called from __del__
            # The python interpreter has nuked most attributes already
            return
        else:
            self._closing = True

    # pylint: disable=no-dunder-del
    def __del__(self):
        self.close()

    # pylint: enable=no-dunder-del

    def timeout_message(self, message):
        """
        Handle a message timeout by removing it from the sending queue
        and informing the caller

        :raises: SaltReqTimeoutError
        """
        future = self.send_future_map.pop(str(hash(message)), None)
        # In a race condition the message might have been sent by the time
        # we're timing it out. Make sure the future is not None
        if future is not None:
            if future.attempts < future.tries:
                future.attempts += 1
                log.debug(
                    "SaltReqTimeoutError, retrying. (%s/%s)",
                    future.attempts,
                    future.tries,
                )
                self.send(
                    message,
                    timeout=future.timeout,
                    tries=future.tries,
                    reply_future=future,
                )

            else:
                future.set_exception(SaltReqTimeoutError("Message timed out"))

    @salt.ext.tornado.gen.coroutine
    def send(
        self,
        message,
        timeout=None,
        tries=3,
        reply_future=None,
        callback=None,
    ):
        """
        Return a future which will be completed when the message has a response
        """

        yield self._rmq_non_blocking_connection_wrapper.connect()

        if reply_future is None:
            reply_future = salt.ext.tornado.concurrent.Future()
            reply_future.tries = tries
            reply_future.attempts = 0
            reply_future.timeout = timeout
            # if a future wasn't passed in, we need to serialize the message
            message = salt.payload.dumps(message)

        if callback is not None:

            def handle_future(future):
                response = future.result()
                self.io_loop.add_callback(callback, response)

            reply_future.add_done_callback(handle_future)

        # Add this future to the mapping; use message hash as key
        correlation_id = str(hash(message))
        self.send_future_map[correlation_id] = reply_future

        if self.opts.get("detect_mode") is True:
            # This code path is largely untested in the product
            timeout = 5

        if timeout is not None:
            send_timeout = self.io_loop.call_later(
                timeout, self.timeout_message, message
            )

        def mark_reply_future(reply_msg, corr_id):
            # reference the mutable map in this closure so that we complete the correct future,
            # as this callback can be called multiple times for multiple invocations of the outer send() function
            future = self.send_future_map[corr_id]
            if not future.done():
                data = salt.payload.loads(reply_msg)
                future.set_result(data)
                self.send_future_map.pop(corr_id)

        # send message and consume reply; callback must be configured first when using amq.rabbitmq.reply-to pattern
        # See https://www.rabbitmq.com/direct-reply-to.html
        consumer_tag = yield self._rmq_non_blocking_connection_wrapper.consume_reply(
            mark_reply_future, reply_queue_name="amq.rabbitmq.reply-to"
        )

        yield self._rmq_non_blocking_connection_wrapper.publish(
            message,
            # Use a special reserved direct-reply queue. See https://www.rabbitmq.com/direct-reply-to.html
            reply_queue_name="amq.rabbitmq.reply-to",
        )

        recv = yield reply_future
        raise salt.ext.tornado.gen.Return(recv)
