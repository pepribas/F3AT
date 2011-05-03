# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4

from zope.interface import Interface, Attribute

__all__ = ("IListener", "IConnectionFactory", "IAgencyAgentInternal",
           "IAgencyInitiatorFactory", "IAgencyInterestFactory",
           "IAgencyInterestInternalFactory",
           "IAgencyInterestInternal", "IAgencyInterestedFactory",
           "IMessagingClient", "IMessagingPeer", "IDatabaseClient",
           "DatabaseError", "ConflictError", "NotFoundError",
           "IFirstMessage", "IDialogMessage", "IDbConnectionFactory",
           "IDatabaseDriver")


class DatabaseError(RuntimeError):
    '''
    Base class for database specific exceptions
    '''


class ConflictError(DatabaseError):
    '''
    Raised when we encounter revision mismatch.
    '''


class NotFoundError(DatabaseError):
    '''
    Raised when we request document which is not there
    or has been deleted.
    '''


class IListener(Interface):
    '''Represents sth which can be registered in AgencyAgent to
    listen for message'''

    def on_message(message):
        '''hook called when message arrives'''

    def get_session_id():
        '''
        @returns: session_id to bound to
        @rtype: string
        '''

    def get_agent_side():
        '''
        @returns: the instance of agent-side protocol
        '''

    def notify_finish():
        '''
        @returns: Deferred which will be run
                  after the protocol has finished
        '''


class IAgencyAgentInternal(Interface):
    '''Internal interface of an agency agent.'''

    def get_agent():
        pass

    def create_binding(prot_id, shard):
        pass

    def register_listener(medium):
        pass

    def unregister_listener(session_id):
        pass

    def send_msg(recipients, msg, handover=False):
        pass

    def journal_protocol_created(factory, medium, *args, **kwargs):
        pass


class IAgencyInterestFactory(Interface):
    '''Factory constructing L{IAgencyInterest} instances.'''

    def __call__(factory):
        '''Creates a new agency interest
        for the specified agent-side factory.'''


class IAgencyInterestInternalFactory(Interface):
    '''Factory constructing L{IAgencyInterestInternal} instances.'''

    def __call__(agency_agent):
        '''Creates a new internal agency interest
        for the specified agent-side factory.'''


class IAgencyInterestInternal(Interface):

    factory = Attribute("Agent-side protocol factory.")

    def bind(shard):
        '''Create a binding for the specified shard.'''

    def revoke():
        '''Revoke the current bindings to the current shard.'''

    def schedule_message(message):
        '''Schedules the handling of a the specified message.'''

    def clear_queue():
        '''Clears the message queue.'''

    def wait_finished():
        '''Returns a Deferred that will be fired when there is no more
        active or queued messages.'''

    def is_idle():
        '''Returns True if there is no active or queued messages.'''


class IAgencyInitiatorFactory(Interface):
    '''Factory constructing L{IAgencyInitiator} instance.'''

    def __call__(agency_agent, recipients, *args, **kwargs):
        '''Creates a new agency initiator
        for the specified agent-side factory.'''


class IAgencyInterestedFactory(Interface):
    '''Factory constructing L{IAgencyInterested} instance.'''

    def __call__(agency_agent, message):
        '''Creates a new agency interested
        for the specified agent-side factory.'''


class IConnectionFactory(Interface):
    '''
    Responsible for creating connection to messaging server.
    Should be implemented by messaging drivers passed to the agency.
    '''

    def get_connection(agent):
        '''
        Instantiate the connection for the agent.

        @params agent: Agent to connect to.
        @type agent: L{feat.agency.interfaces.IMessagingPeer}
        @returns: L{IMessagingClient}
        '''


class IMessagingClient(Interface):
    '''
    Interface used by L{IAgencyAgent} to send messages and maintain bindings.
    '''

    def publish(key, shard, message):
        '''
        Send message.

        @param key: Will be passed to the exchange as the routing key.
        @param shard: Identifier of the exchange we are sending message to.
        @param message: Message body.
        @type message: subclass of L{feat.agents.message.BaseMessage}
        '''

    def disconnect():
        '''
        Disconnect client from messaging server.
        '''

    def personal_binding(key, shard):
        '''
        Creates a personal binding (direct binding of the key to personal
        agents queue).

        @param key: Routing key to bind to.
        @param shard: Optional. Shard identifier to bind in. If None uses
                      the agents shard.
        '''

    def get_bindings(shard):
        '''
        Returns the list of binding maintained by the messaging client.

        @param shard: Optional. If specified limits the result to the selected
                      shard. If None return all.
        @returns: List of subclasses of bindings.
        @return_type: list
        '''


class IMessagingPeer(Interface):
    '''
    Interface which agent needs to implement to use messaging connection.
    Required by (feat.agencies.messaging.Connection)
    '''

    def get_queue_name():
        '''
        Return the name of the queue to listen too.
        Return value of None means: do not create any queue or consumer.
        '''

    def get_shard_name():
        '''
        Return the name of exchange to bind to.
        '''

    def on_message(message):
        '''
        Callback called after the message arrives.
        '''


class IFirstMessage(Interface):
    '''
    This interface needs to be implemeneted by the message object which is
    the first one in the dialog. Implemeneted by: Announcement, Request.
    '''

    traversal_id = Attribute('Unique identifier. It is preserved during '
                             'nesting between shard, to detect duplications.')


class IDbConnectionFactory(Interface):
    '''
    Responsible for creating connection to database server.
    Should be implemented by database drivers passed to the agency.
    '''

    def get_connection():
        '''
        Instantiate the connection for the agent.

        @returns: L{IDatabaseClient}
        '''


class IDialogMessage(Interface):
    '''
    This interface needs to be implemeneted by the message
    objects which take part on a dialog.
    '''

    reply_to = Attribute("The recipient to send the response to.")
    sender_id = Attribute("The sender unique identifier.")
    receiver_id = Attribute("The receiver unique identifier.")


class IDatabaseClient(Interface):

    def save_document(document):
        '''
        Save the document into the database. Document might have been loaded
        from the database before, or has just been constructed.

        If the doc_id
        property of the document is not set, it will be loaded from the
        database.

        @param document: Document to be saved.
        @type document: Subclass of L{feat.agents.document.Document}
        @returns: Deferred called with the updated Document (id and revision
                  set)
        '''

    def get_document(document_id):
        '''
        Download the document from the database and instantiate it.
        The document should have the 'document_type' basing on which we decide
        which subclass of L{feat.agents.document.Document} to instantiate.

        @param document_id: The id of the document in the database.
        @returns: The Deffered called with the instance representing downloaded
                  document.
        '''

    def reload_document(document):
        '''
        Fetch the latest revision of the document and update it.

        @param document: Document to update.
        @type document: Subclass of L{feat.agents.document.Document}.
        @returns: Deferred called with the updated instance.
        '''

    def delete_document(document):
        '''
        Marks the document in the database as deleted. The document
        returns in the deferred can still be used in the application.
        For example one can call save_document on it to bring it back.

        @param document: Document to be deleted.
        @type document: Subclass of L{feat.agents.document.Document}.
        @returns: Deferred called with the updated document (latest revision).
        '''

    def changes_listener(doc_ids, callback):
        '''
        Register a callback called when the document is changed.
        If different=True (defualt) only changes triggered by this session
        are ignored.
        @param document: Document ids to look to
        @param callback: Callable to call
        @param different: Flag telling whether to ignore changes triggered
                          by this session.
        '''

    def disconnect():
        '''
        Disconnect from database server.
        '''

    def create_database():
        '''
        Request creating the database.
        '''


class IDatabaseDriver(Interface):
    '''
    Interface implemeneted by the database driver.
    '''

    def create_db():
        '''
        Request creating the database.
        '''

    def save_doc(doc, doc_id=None):
        '''
        Create new or update existing document.
        @param doc: string with json document
        @param doc_id: id of the document
        @return: Deferred fired with the HTTP response body (keys: id, rev)
        '''

    def open_doc(doc_id):
        '''
        Fetch document from database.
        @param doc_id: id of the document to fetch
        @return: Deferred fired with json parsed document.
        '''

    def delete_doc(doc_id, revision):
        '''
        Mark document as delete.
        @param doc_id: id of document to delete
        @param revision: revision of the document
        @return: Deferred fired with dict(id, rev) or errbacked with
                 ConflictError
        '''

    def listen_changes(doc_ids, callback):
        '''
        Register callback called when one of the documents get changed.
        @param doc_ids: list of document ids which we are interested in
        @param callback: callback to call, it will get doc_id and revision
        @return: Deferred trigger with unique listener identifier
        @rtype: Deferred
        '''

    def cancel_listener(listener_id):
        '''
        Unregister callback called on document changes.
        @param listener_id: Id returned buy listen_changes() method
        @rtype: Deferred
        @return: Deferred which will fire when the listener is cancelled.
        '''
