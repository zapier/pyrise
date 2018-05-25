from __future__ import unicode_literals
import re
import sys
from datetime import datetime, timedelta
from xml.etree import ElementTree

import requests

from six import text_type
from six.moves.urllib.parse import quote


class Highrise:
    """Class designed to handle all interactions with the Highrise API."""

    _server = None
    _tzoffset = 0

    @classmethod
    def auth(cls, token):
        """Define the settings used to connect to Highrise"""

        cls.token = token

    @classmethod
    def set_server(cls, server):
        """Define the server to be used for API requests"""

        if server[:4] == 'http':
            cls._server = server.strip('/')
        else:
            cls._server = "https://{}.highrisehq.com".format(server)

    @classmethod
    def set_timezone_offset(cls, offset):
        """Rather than force pytz or some other time zone library, Pyrise
        works entirely in GMT (as does the Highrise API). Setting this
        optional offset value will let you compensate for your local
        server timezone, if desired"""

        cls._tzoffset = offset

    @classmethod
    def from_utc(cls, date):
        """Convert a date from UTC using the _tzoffset value"""

        return date + timedelta(hours=cls._tzoffset)

    @classmethod
    def to_utc(cls, date):
        """Convert a date to UTC using the _tzoffset value"""

        return date - timedelta(hours=cls._tzoffset)

    @classmethod
    def parseurl(cls, val):
        """This is for Python 3/2 support."""

        # Functor to ensure that str is encoded to UTF8 before being used as a URL parameter
        utf8 = lambda s: s.encode("utf8") if isinstance( s, str ) else s
        return quote(utf8(val))

    @classmethod
    def request(cls, path, method='GET', xml=None, hooks=None, **request_kwargs):
        """Process an arbitrary request to Highrise.

        Ordinarily, you shouldn't have to call this method directly,
        but it's available to send arbitrary requests if needed."""

        # build the base request URL
        url = '{}/{}'.format(cls._server, path.strip('/'))

        # make the request
        kwargs = {'auth': (cls.token, 'X')}
        kwargs.update(request_kwargs)

        if xml:
            kwargs['data'] = xml
            kwargs['headers'] = {'Content-Type': 'application/xml'}
        if method == 'GET':
            r = requests.get(url, **kwargs)
        elif method == 'POST':
            r = requests.post(url, **kwargs)
        elif method == 'PUT':
            r = requests.put(url, **kwargs)
        elif method == 'DELETE':
            r = requests.delete(url, **kwargs)

        # raise appropriate exceptions if there is an error
        if r.status_code >= 400:
            if r.status_code == 400:
                raise BadRequest
            elif r.status_code == 401:
                raise AuthorizationRequired(r.text)
            elif r.status_code == 403:
                raise Forbidden(r.text)
            elif r.status_code == 404:
                raise NotFound(r.text)
            elif r.status_code == 422:
                raise GatewayFailure(r.text)
            elif r.status_code == 502:
                raise GatewayConnectionError(r.text)
            elif r.status_code == 507:
                raise InsufficientStorage(r.text)
            else:
                raise UnexpectedResponse(r.text)

        if hooks and 'response' in hooks:
            hooks['response'](r)

        # if this was a PUT or DELETE request, return status (hopefully success)
        if method in ('PUT', 'DELETE'):
            return r.status_code

        # for GET and POST requests, return the XML response
        try:
            return ElementTree.fromstring(r.text)
        except Exception:
            raise UnexpectedResponse("The server sent back something that wasn't valid XML.")

    @classmethod
    def key_to_class(cls, key):
        """Utility method to convert a hyphenated key (like what is used
        in Highrise XML responses) to a Python class name"""

        klass = key.capitalize()
        while '-' in klass:
            ix = klass.index('-')
            next = klass[ix + 1].upper()
            klass = klass[0:ix] + next + klass[ix + 2:]

        return klass

    @classmethod
    def class_to_key(cls, key):
        """Utility method to convert a Python class name to a hyphenated
        key (like what is used in Highrise XML responses)"""

        match = re.search(r'([A-Z])', key)
        while match:
            char = match.groups()[0]
            key = key.replace(char, '-' + char.lower())
            match = re.search(r'([A-Z])', key)

        return key[1:]


class HighriseObject(object):
    """Base class for all Highrise data objects"""

    @classmethod
    def from_xml(cls, xml, parent=None):
        """Create a new object from XML data"""

        # instiantiate the object
        if cls == Party:
            cls = getattr(sys.modules[__name__], xml.get('type'))
        self = cls()

        for child in xml:
            # convert the key to underscore notation for Python
            key = child.tag.replace('-', '_')

            # if this key is not recognized by pyrise, ignore it
            if key not in cls.fields:
                continue

            # if there is no data, just set the default
            if child.text is None:
                self.__dict__[key] = self.fields[key].default
                continue

            # handle the contact-data key differently
            if key == 'contact_data':
                klass = getattr(sys.modules[__name__], 'ContactData')
                self.contact_data = klass.from_xml(child, parent=self)
                continue

            # if this an element with children, it's an object relationship
            if len(list(child)) > 0:
                # is this element an array of objects?
                if cls.fields[key].type == list:
                    items = []
                    for item in child:
                        if item.tag == 'party':
                            class_string = item.find('type').text
                        else:
                            class_string = Highrise.key_to_class(item.tag.replace('_', '-'))
                        klass = getattr(sys.modules[__name__], class_string)
                        items.append(klass.from_xml(item, parent=self))
                    self.__dict__[child.tag.replace('-', '_')] = items
                    continue

                # otherwise, let's treat it like a single object
                else:
                    if child.tag == 'party':
                        class_string = child.find('type').text
                    else:
                        class_string = Highrise.key_to_class(child.tag)
                    klass = getattr(sys.modules[__name__], class_string)
                    self.__dict__[child.tag.replace('-', '_')] = klass.from_xml(child, parent=self)
                    continue

            # get and convert attribute value based on type
            data_type = child.get('type')
            if data_type == 'integer':
                value = int(child.text)
            elif data_type == 'datetime':
                value = Highrise.from_utc(datetime.strptime(child.text, '%Y-%m-%dT%H:%M:%SZ'))
            else:
                value = text_type(child.text)

            # add value to object dictionary
            self.__dict__[key] = value

        return self

    @classmethod
    def _list(cls, path, tag):
        """Get a list of objects of this type from Highrise"""

        # retrieve the data from Highrise
        objects = []
        xml = Highrise.request(path)

        # make a list of objects and return it
        for item in xml.iter(tag):
            objects.append(cls.from_xml(item))

        return objects

    def __init__(self, parent=None, **kwargs):
        """Create a new object manually."""

        self._server = Highrise._server
        for field, settings in self.fields.items():
            if field in kwargs:
                if not settings.is_editable:
                    raise KeyError('{} is not an editable attribute'.format(field))
                value = kwargs.pop(field)
            else:
                value = settings.default
            self.__dict__[field] = value

    def save_xml(self, include_id=False, **kwargs):
        """Return the object XML for sending back to Highrise"""

        # create new XML object
        if 'base_element' not in kwargs:
            kwargs['base_element'] = Highrise.class_to_key(self.__class__.__name__)
        xml = ElementTree.Element(kwargs['base_element'])

        extra_attrs = kwargs.get('extra_attrs', {})

        # if the id should be included and it is not None, add it first
        if include_id and 'id' in self.__dict__ and self.id != None:
            id_element = ElementTree.SubElement(xml, tag='id', attrib={'type': 'integer'})
            id_element.text = text_type(self.id)

        # now iterate over the editable attributes
        for field, settings in self.fields.items():
            # get the value for this field, or pass if it is missing
            if field in self.__dict__:
                value = self.__dict__[field]
            else:
                continue

            # if the field is not editable, don't pass it
            if not settings.is_editable:
                continue

            # if the value is equal to the default, don't pass it
            if value == settings.default:
                continue

            # if the value is a HighriseObject, insert the XML for it
            if isinstance(value, HighriseObject):
                xml.insert(0, value.save_xml(include_id=True))
                continue

            field_name = field.replace('_', '-') if not settings.force_key else settings.force_key
            extra_attrs_copy = extra_attrs if not settings.extra_attrs else settings.extra_attrs

            # insert the remaining single-attribute elements
            e = ElementTree.Element(field_name, **extra_attrs_copy)
            if isinstance(value, int):
                e.text = text_type(value)
            elif isinstance(value, list):
                if len(value) == 0:
                    continue
                for item in value:
                    e.insert(0, item.save_xml(include_id=True))
            elif isinstance(value, datetime):
                e.text = datetime.strftime(Highrise.to_utc(value), '%Y-%m-%dT%H:%M:%SZ')
            else:
                e.text = value
            xml.insert(0, e)

        # return the final XML Element object
        return xml


class HighriseField(object):
    """An object to represent the settings for an object attribute
    Note that a lot more detail could go into how this works."""

    def __init__(self, type='uneditable', options=None, **kwargs):
        self.type = type
        self.options = options
        self.force_key = kwargs.pop('force_key', None)
        self.extra_attrs = kwargs.pop('extra_attrs', None)

    @property
    def default(self):
        """Return the default value for this data type (e.g. '' or [])"""

        if self.type in ('id', 'uneditable'):
            return None
        elif self.type == datetime:
            return datetime.now()
        else:
            return self.type()

    @property
    def is_editable(self):
        """Boolean flag for whether or not this field is editable"""

        return self.type not in ('id', 'uneditable')


class SubjectField(HighriseObject):
    """An object representing a Highise custom field."""

    fields = {
        'id': HighriseField(type='id'),
        'label': HighriseField(),
    }

    @classmethod
    def all(cls):
        """Get all custom fields"""

        return cls._list('subject_fields.xml', 'subject-field')

class Tag(HighriseObject):
    """An object representing a Highrise tag."""

    fields = {
        'id': HighriseField(type='id'),
        'name': HighriseField(),
    }

    @classmethod
    def all(cls):
        """Get all tags"""

        return cls._list('tags.xml', 'tag')

    @classmethod
    def get_by(cls, subject, subject_id):
        """Get tags for a specific person, company, case, or deal"""

        return cls._list('{}/{}/tags.xml'.format(subject, subject_id), 'tag')

    @classmethod
    def add_to(cls, subject, subject_id, name):
        """Add a tag to a specific person, company, case, or deal"""
        xml = ElementTree.Element('name')
        xml.text = name
        xml_string = ElementTree.tostring(xml, encoding=None)

        response = Highrise.request('{}/{}/tags.xml'.format(subject, subject_id), method='POST', xml=xml_string)
        return cls.from_xml(response)

    @classmethod
    def remove_from(cls, subject, subject_id, tag_id):
        """Add a tag to a specific person, company, case, or deal"""

        return Highrise.request('{}/{}/tags/{}.xml'.format(subject, subject_id, tag_id), method='DELETE')


class Message(HighriseObject):
    """An object representing a Highrise email or note."""

    def __new__(cls, extended_fields={}, **kwargs):
        """Set object attributes for subclasses of Party (companies and people)"""

        # set the base fields dictionary and extend it with any additional fields
        cls.fields = {
            'id': HighriseField(type='id'),
            'body': HighriseField(type=str),
            'author_id': HighriseField(),
            'subject_id': HighriseField(type=int),
            'subject_type': HighriseField(type=str, options=('Party', 'Deal', 'Kase')),
            'subject_name': HighriseField(),
            'collection_id': HighriseField(type=int),
            'collection_type': HighriseField(type=str, options=('Deal', 'Kase')),
            'visible_to': HighriseField(type=str, options=('Everyone', 'Owner', 'NamedGroup')),
            'owner_id': HighriseField(type=int),
            'group_id': HighriseField(type=int),
            'created_at': HighriseField(type=datetime),
            'updated_at': HighriseField(),
        }
        cls.fields.update(extended_fields)

        # send back the object reference
        return HighriseObject.__new__(cls)

    @classmethod
    def get(cls, id):
        """Get a single message"""

        # retrieve the data from Highrise
        xml = Highrise.request('/{}/{}.xml'.format(cls.plural, id))

        # return a note object
        for obj_xml in xml.iter(tag=cls.singular):
            return cls.from_xml(obj_xml)

    @classmethod
    def filter(cls, **kwargs):
        """Get a list of messages based by subject"""

        # map kwarg to URL slug for request
        kwarg_to_path = {
            'person': 'people',
            'company': 'companies',
            'kase': 'kases',
            'deal': 'deals',
        }

        # find the first kwarg that we understand and use it to generate the request path
        for key, value in kwargs.items():
            if key in kwarg_to_path:
                path = '/{}/{}/{}.xml'.format(kwarg_to_path[key], value, cls.plural)
                break
        else:
            raise KeyError('filter method must have person, company, kase, or deal as an kwarg')

        # return the list of messages from Highrise
        return cls._list(path, cls.singular)

    def save(self, **kwargs):
        """Save a message to Highrise."""

        # get the XML for the request
        xml = self.save_xml()
        xml_string = ElementTree.tostring(xml, encoding=None)

        # if this was an initial save, update the object with the returned data
        if self.id == None:
            response = Highrise.request('/{}.xml'.format(self.plural), method='POST', xml=xml_string, **kwargs)
            new = self.from_xml(response)

        # if this was a PUT request, we need to re-request the object
        # so we can get any new ID values set at ceation
        else:
            response = Highrise.request('/{}/{}.xml'.format(self.plural, self.id), method='PUT', xml=xml_string, **kwargs)
            new = cls.get(self.id)

        # update the values of self to align with what came back from Highrise
        self.__dict__ = new.__dict__

    def delete(self):
        """Delete a message from Highrise."""

        return Highrise.request('/{}/{}.xml'.format(self.plural, self.id), method='DELETE')


class Note(Message):
    """An object representing a Highrise note"""

    plural = 'notes'
    singular = 'note'


class Email(Message):
    """An object representing a Highrise email"""

    plural = 'emails'
    singular = 'email'

    def __new__(cls, **kwargs):
        extended_fields = {
            'title': HighriseField(type=str),
        }
        return Message.__new__(cls, extended_fields, **kwargs)


class Deal(HighriseObject):
    """An object representing a Highrise deal."""

    fields = {
        'id': HighriseField(type='id'),
        'account_id': HighriseField(),
        'author_id': HighriseField(),
        'background': HighriseField(type=str),
        'category_id': HighriseField(type=int),
        'visible_to': HighriseField(type=str, options=('Everyone', 'Owner', 'NamedGroup')),
        'owner_id': HighriseField(type=int),
        'group_id': HighriseField(type=int),
        'created_at': HighriseField(),
        'updated_at': HighriseField(),
        'currency': HighriseField(type=str),
        'duration': HighriseField(type=int),
        'name': HighriseField(type=str),
        'price': HighriseField(type=int),
        'price_type': HighriseField(type=str, options=('fixed', 'hour', 'month', 'year')),
        'responsible_party_id': HighriseField(type=int),
        'status': HighriseField(type=str, options=('pending', 'won', 'lost')),
        'status_changed_on': HighriseField(),
        'parties': HighriseField(type=list),
        'party': HighriseField(),
        'party_id': HighriseField(type=int),
    }

    @classmethod
    def all(cls):
        """Get all deals"""

        return cls._list('deals.xml', 'deal')

    @classmethod
    def get(cls, id):
        """Get a single deal"""

        # retrieve the deal from Highrise
        xml = Highrise.request('/deals/{}.xml'.format(id))

        # return a deal object
        for deal_xml in xml.iter(tag='deal'):
            return Deal.from_xml(deal_xml)

    @property
    def notes(self):
        """Get the notes associated with this deal"""

        # sanity check: has this deal been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the deal before you can load its notes')

        # get the notes
        return Note.filter(deal=self.id)

    @property
    def tasks(self):
        """Get the tasks associated with this deal"""

        # sanity check: has this deal been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the deal before you can load its tasks')

        # get the notes
        return Task.filter(deal=self.id)

    @property
    def emails(self):
        """Get the emails associated with this deal"""

        # sanity check: has this deal been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the deal before you can load its emails')

        # get the emails
        return Email.filter(deal=self.id)

    def save(self, **kwargs):
        """Save a deal to Highrise."""

        # get the XML for the request
        xml = self.save_xml()
        xml_string = ElementTree.tostring(xml, encoding=None)

        # if this was an initial save, update the object with the returned data
        if self.id == None:
            response = Highrise.request('/deals.xml', method='POST', xml=xml_string, **kwargs)
            new = Deal.from_xml(response)

        # if this was a PUT request, we need to re-request the object
        # so we can get any new ID values set at ceation
        else:
            response = Highrise.request('/deals/{}.xml'.format(self.id), method='PUT', xml=xml_string, **kwargs)
            new = Deal.get(self.id)

        # update the values of self to align with what came back from Highrise
        self.__dict__ = new.__dict__

    def set_status(self, status):
        """Change the status of a deal"""

        # prepare the XML string for submission
        xml = ElementTree.Element('status')
        xml_name = ElementTree.Element('name')
        xml_name.text = status
        xml.insert(0, xml_name)
        xml_string = ElementTree.tostring(xml, encoding=None)

        # submit the PUT request
        response = Highrise.request('/deals/{}/status.xml'.format(self.id), method='PUT', xml=xml_string)

    def add_note(self, body, **kwargs):
        """Add a note to a deal"""

        # sanity check: has this deal been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the deal before you can add a note')

        # add the note and save it to Highrise
        note = Note(body=body, subject_id=self.id, subject_type='Deal', **kwargs)
        note.save()

    def add_email(self, title, body, **kwargs):
        """Add an email to a deal"""

        # sanity check: has this deal been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the deal before you can add an email')

        # add the email and save it to Highrise
        email = Email(title=title, body=body, subject_id=self.id, subject_type='Deal', **kwargs)
        email.save()

    def delete(self):
        """Delete a deal from Highrise."""

        return Highrise.request('/deals/{}.xml'.format(self.id), method='DELETE')



class Task(HighriseObject):
    """An object representing a Highrise task."""

    plural = 'tasks'
    singular = 'task'

    fields = {
        'id': HighriseField(type='id'),
        'recording_id': HighriseField(type=int),
        'subject_id': HighriseField(type=int),
        'subject_type': HighriseField(type=str, options=('Party')),
        'category_id': HighriseField(type=int),
        'body': HighriseField(type=str),
        'frame': HighriseField(type=str, options=('specific')),
        'due_at': HighriseField(type=datetime),
        'alert_at': HighriseField(type=datetime),
        'created_at': HighriseField(type=datetime),
        'author_id': HighriseField(type=int),
        'updated_at': HighriseField(type=datetime),
        'public': HighriseField(type=bool),
        'owner_id': HighriseField(type=int),
        'notify': HighriseField(type=bool),
    }

    @classmethod
    def all(cls):
        """Get all tasks"""

        return cls._list('tasks.xml', 'task')

    @classmethod
    def get(cls, id):
        """Get a single task"""

        # retrieve the task from Highrise
        xml = Highrise.request('/tasks/{}.xml'.format(id))

        # return a task object
        for task_xml in xml.iter(tag='task'):
            return Task.from_xml(task_xml)

    def save(self, **kwargs):
        """Save a task to Highrise."""

        # get the XML for the request
        xml = self.save_xml()
        xml_string = ElementTree.tostring(xml, encoding=None)

        # if this was an initial save, update the object with the returned data
        if self.id == None:
            response = Highrise.request('/tasks.xml', method='POST', xml=xml_string, **kwargs)
            new = Task.from_xml(response)

        # if this was a PUT request, we need to re-request the object
        # so we can get any new ID values set at ceation
        else:
            response = Highrise.request('/tasks/{}.xml'.format(self.id), method='PUT', xml=xml_string, **kwargs)
            new = Task.get(self.id)

        # update the values of self to align with what came back from Highrise
        self.__dict__ = new.__dict__

    def delete(self):
        """Delete a task from Highrise."""

        return Highrise.request('/tasks/{}.xml'.format(self.id), method='DELETE')

    @classmethod
    def filter(cls, **kwargs):
        """Get a list of tasks based by subject"""

        # map kwarg to URL slug for request
        kwarg_to_path = {
            'person': 'people',
            'company': 'companies',
            'kase': 'kases',
            'deal': 'deals',
        }

        # find the first kwarg that we understand and use it to generate the request path
        for key, value in kwargs.items():
            if key in kwarg_to_path:
                path = '/{}/{}/{}.xml'.format(kwarg_to_path[key], value, cls.plural)
                break
        else:
            raise KeyError('filter method must have person, company, kase, or deal as an kwarg')

        # return the list of messages from Highrise
        return cls._list(path, cls.singular)


class ContactData(HighriseObject):
    """An object representing contact data for a
    Highrise person or company."""

    fields = {
        'email_addresses': HighriseField(type=list),
        'phone_numbers': HighriseField(type=list),
        'addresses': HighriseField(type=list),
        'instant_messengers': HighriseField(type=list),
        'twitter_accounts': HighriseField(type=list),
        'web_addresses': HighriseField(type=list),
    }

    def save(self):
        """Save the parent parent person or company"""

        return NotImplemented


class ContactDetail(HighriseObject):
    """A base class for contact details"""

    def save(self):
        """Save the parent person or company this detail belongs to"""

        return NotImplemented


class EmailAddress(ContactDetail):
    """An object representing an email address"""

    fields = {
        'id': HighriseField(type='id'),
        'address': HighriseField(type=str),
        'location': HighriseField(type=str, options=('Work', 'Home', 'Other')),
    }


class PhoneNumber(ContactDetail):
    """An object representing an phone number"""

    fields = {
        'id': HighriseField(type='id'),
        'number': HighriseField(type=str),
        'location': HighriseField(type=str, options=('Work', 'Mobile', 'Fax', 'Pager', 'Home', 'Skype', 'Other')),
    }


class Address(ContactDetail):
    """An object representing a physical address"""

    fields = {
        'id': HighriseField(type='id'),
        'city': HighriseField(type=str),
        'country': HighriseField(type=str),
        'state': HighriseField(type=str),
        'zip': HighriseField(type=str),
        'street': HighriseField(type=str),
        'location': HighriseField(type=str, options=('Work', 'Home', 'Other')),
    }


class InstantMessenger(ContactDetail):
    """An object representing an instant messanger"""

    fields = {
        'id': HighriseField(type='id'),
        'address': HighriseField(type=str),
        'protocol': HighriseField(type=str, options=('AIM', 'MSN', 'ICQ', 'Jabber', 'Yahoo', 'Skype', 'QQ', 'Sametime', 'Gadu-Gadu', 'Google Talk', 'other')),
        'location': HighriseField(type=str, options=('Work', 'Personal', 'Other')),
    }


class TwitterAccount(ContactDetail):
    """An object representing an Twitter account"""

    fields = {
        'id': HighriseField(type='id'),
        'username': HighriseField(type=str),
        'location': HighriseField(type=str, options=('Work', 'Personal', 'Other')),
    }


class WebAddress(ContactDetail):
    """An object representing a web address"""

    fields = {
        'id': HighriseField(type='id'),
        'url': HighriseField(type=str),
        'location': HighriseField(type=str, options=('Work', 'Personal', 'Other')),
    }



class SubjectData(HighriseObject):
    """An object representing an email address"""

    fields = {
        'id': HighriseField(type='id'),
        'subject_field_id': HighriseField(type=int, force_key='subject_field_id', extra_attrs={'type': 'integer'}),
        'subject_field_label': HighriseField(type=str),
        'value': HighriseField(type=str)
    }

    def save_xml(self, *args, **kwargs):
        kwargs['base_element'] = 'subject_data'
        return super(SubjectData, self).save_xml(*args, **kwargs)

class Case(HighriseObject):
    """An object representing a Highrise Case."""

    fields = {
        'id': HighriseField(type='id'),
        'author_id': HighriseField(type=int),
        'closed_at': HighriseField(type=datetime),
        'created_at': HighriseField(type=datetime),
        'updated_at': HighriseField(type=datetime),
        'name': HighriseField(type=str),
        'visible-to': HighriseField(type=str),
        'group_id': HighriseField(type=int),
        'owner_id': HighriseField(type=int),
        'parties': HighriseField(type=list)
    }

    @classmethod
    def all(cls):
        """Get all cases"""

        return cls._list('kases/open.xml', 'kase')

    @classmethod
    def get(cls, id):
        """Get a single case"""

        # retrieve the case from Highrise
        xml = Highrise.request('/kases/{}.xml'.format(id))

        # return a case object
        for case_xml in xml.getiterator(tag='kase'):
            return Case.from_xml(case_xml)

    def save(self):
        """Save a case to Highrise."""

        # get the XML for the request
        xml = self.save_xml()
        xml_string = ElementTree.tostring(xml, encoding=None)

        # if this was an initial save, update the object with the returned data
        if self.id == None:
            response = Highrise.request('/kases.xml', method='POST', xml=xml_string)
            new = Case.from_xml(response)

        # if this was a PUT request, we need to re-request the object
        # so we can get any new ID values set at creation
        else:
            response = Highrise.request('/kases/{}.xml'.format(self.id), method='PUT', xml=xml_string)
            new = Case.get(self.id)

        # update the values of self to align with what came back from Highrise
        self.__dict__ = new.__dict__

    def delete(self):
        """Delete a task from Highrise."""

        return Highrise.request('/kases/{}.xml'.format(self.id), method='DELETE')

class Party(HighriseObject):
    """An object representing a Highrise person or company."""

    singular = 'party'
    plural = 'parties'

    def __new__(cls, extended_fields={}, **kwargs):
        """Set object attributes for subclasses of Party (companies and people)"""

        # set the base fields dictionary and extend it with any additional fields
        cls.fields = {
            'id': HighriseField(type='id'),
            'background': HighriseField(type=str),
            'visible_to': HighriseField(type=str, options=('Everyone', 'Owner', 'NamedGroup')),
            'owner_id': HighriseField(type=int),
            'group_id': HighriseField(type=int),
            'contact_data': HighriseField(type=ContactData),
            'avatar_url': HighriseField(type=str),
            'author_id': HighriseField(),
            'created_at': HighriseField(),
            'updated_at': HighriseField()
        }
        cls.fields.update(extended_fields)

        # send back the object reference
        return HighriseObject.__new__(cls)

    @classmethod
    def all(cls, offset=None):
        """Get all parties"""

        if offset:
            return cls._list('{}.xml?n={}'.format(cls.plural, offset), cls.singular)
        else:
            return cls._list('{}.xml'.format(cls.plural), cls.singular)

    @classmethod
    def filter(cls, **kwargs):
        """Get a list of parties based on filter criteria"""

        # if company_id or title are present in kwargs, we should be running
        # this against the Person object directly
        if ('company_id' in kwargs or 'title' in kwargs):
            return Person._filter(**kwargs)

        paging = ''
        if 'n' in kwargs:
            n = kwargs.pop('n')
            paging = '&n=%d' % n

        # get the path for filter methods that only take a single argument
        if 'term' in kwargs:
            path = '/{}/search.xml?term={}'.format(cls.plural, Highrise.parseurl(kwargs['term']))
            if len(kwargs) > 1:
                raise KeyError('"term" can not be used with any other keyward arguments')

        elif 'tag_id' in kwargs:
            path = '/{}.xml?tag_id={}'.format(cls.plural, Highrise.parseurl(kwargs['tag_id']))
            if len(kwargs) > 1:
                raise KeyError('"tag_id" can not be used with any other keyward arguments')

        elif 'since' in kwargs:
            paging = '' # since does not page results
            path = '/{}.xml?since={}'.format(cls.plural, datetime.strftime(Highrise.from_utc(kwargs['since']), '%Y%m%d%H%M%S'))
            if len(kwargs) > 1:
                raise KeyError('"since" can not be used with any other keyward arguments')

        # if we didn't get a single-argument kwarg, process using the search criteria method
        else:
            if kwargs.keys():
                path = '/{}/search.xml?'.format(cls.plural)
                for key in kwargs:
                    path += 'criteria[{}]={}&'.format(key, Highrise.parseurl(kwargs[key]))
                path = path[:-1]
            else:
                # allow filtering by 'n' alone without using search.xml
                path = '/{}.xml?'.format(cls.plural)

        # return the list of people from Highrise
        return cls._list(path+paging, cls.singular)

    @classmethod
    def get(cls, id):
        """Get a single party"""

        # retrieve the person from Highrise
        xml = Highrise.request('/{}/{}.xml'.format(cls.plural, id))

        # return a person object
        for obj_xml in xml.iter(tag=cls.singular):
            return cls.from_xml(obj_xml)

    @property
    def tags(self):
        """Get the tags associated with this party"""

        # sanity check: has this person been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the person before you can load their tags')

        # get the tags
        return Tag.get_by(self.plural, self.id)

    @property
    def notes(self):
        """Get the notes associated with this party"""

        # sanity check: has this person been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the person before you can load their notes')

        # get the notes
        kwargs = {}
        kwargs[self.singular] = self.id
        return Note.filter(**kwargs)

    @property
    def tasks(self):
        """Get the tasks associated with this party"""

        # sanity check: has this person been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the person before you can load their tasks')

        # get the notes
        kwargs = {}
        kwargs[self.singular] = self.id
        return Task.filter(**kwargs)

    @property
    def emails(self):
        """Get the emails associated with this party"""

        # sanity check: has this person been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the person before you can load their emails')

        # get the emails
        kwargs = {}
        kwargs[self.singular] = self.id
        return Email.filter(**kwargs)

    def add_tag(self, name):
        """Add a tag to a party"""

        # sanity check: has this party been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the {} before you can add a tag'.format(self.singular))

        # add the tag
        return Tag.add_to(self.plural, self.id, name)

    def remove_tag(self, tag_id):
        """Remove a tag from a party"""

        # sanity check: has this party been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the {} before you can remove a tag'.format(self.singular))

        # remove the tag
        return Tag.remove_from(self.plural, self.id, tag_id)

    def add_note(self, body, **kwargs):
        """Add a note to a party"""

        # sanity check: has this party been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the {} before you can add a note'.format(self.singular))

        # add the note and save it to Highrise
        note = Note(body=body, subject_id=self.id, subject_type='Party', **kwargs)
        note.save()

    def add_email(self, title, body, **kwargs):
        """Add an email to a party"""

        # sanity check: has this party been saved to Highrise yet?
        if self.id == None:
            raise ElevatorError('You have to save the {} before you can add an email'.format(self.singular))

        # add the email and save it to Highrise
        email = Email(title=title, body=body, subject_id=self.id, subject_type='Party', **kwargs)
        email.save()

    def save(self, **kwargs):
        """Save a party to Highrise."""

        # get the XML for the request
        xml = self.save_xml()
        xml_string = ElementTree.tostring(xml, encoding=None)

        # if this was an initial save, update the object with the returned data
        if self.id == None:
            response = Highrise.request('/{}.xml'.format(self.plural), method='POST', xml=xml_string, **kwargs)
            new = Person.from_xml(response)

        # if this was a PUT request, we need to re-request the object
        # so we can get any new ID values for phone numbers, addresses, etc.
        else:
            response = Highrise.request('/{}/{}.xml'.format(self.plural, self.id), method='PUT', xml=xml_string, **kwargs)
            new = self.get(self.id)

        # update the values of self to align with what came back from Highrise
        self.__dict__ = new.__dict__

    def delete(self):
        """Delete a party from Highrise."""

        return Highrise.request('/{}/{}.xml'.format(self.plural, self.id), method='DELETE')


class Person(Party):
    """An object representing a Highrise person"""

    plural = 'people'
    singular = 'person'

    def __new__(cls, **kwargs):
        extended_fields = {
            'first_name': HighriseField(type=str),
            'last_name': HighriseField(type=str),
            'title': HighriseField(type=str),
            'company_id': HighriseField(type=int),
            'company_name': HighriseField(type=str),
            'subject_datas': HighriseField(type=list, force_key='subject_datas', extra_attrs={'type': 'array'}),
        }
        return Party.__new__(cls, extended_fields, **kwargs)

    @classmethod
    def _filter(cls, **kwargs):
        """Get a list of people based on filter criteria"""

        # get all people in a company
        if 'company_id' in kwargs:
            path = '/companies/{}/people.xml'.format(kwargs['company_id'])
            if len(kwargs) > 1:
                raise KeyError('"company_id" can not be used with any other keyward arguments')

        # get all people will a specific title
        elif 'title' in kwargs:
            path = '/people.xml?title={}'.format(kwargs['title'])
            if len(kwargs) > 1:
                raise KeyError('"title" can not be used with any other keyward arguments')

        # return the list of people from Highrise
        return cls._list(path, 'person')


class Company(Party):
    """An object representing a Highrise company"""

    plural = 'companies'
    singular = 'company'

    def __new__(cls, **kwargs):
        extended_fields = {
            'name': HighriseField(type=str),
            'subject_datas': HighriseField(type=list, force_key='subject_datas', extra_attrs={'type': 'array'}),
        }
        return Party.__new__(cls, extended_fields, **kwargs)


class User(HighriseObject):
    """
    A Highrise account user (NB this is *not* a Person).

    A user is someone who has a Highrise account, and is not the same
    thing as a Person (who is a Highrise contact). Users are the people who
    use the system, rather than those stored within it.

    Having a User class is useful as it allows you to access real names for
    people who have edited / added items to Highrise. For example, if you
    want to know who wrote a Note, then the Note API will return the author_id
    which can be used to look up the User.

    e.g.
    >>> person = pyrise.Person.get(123)
    >>> note = person.notes[0]
    >>> author = pyrise.User.get(note.author_id)
    >>> print author.name + ' wrote the note.'
    Bob wrote the note.

    API docs available at https://github.com/37signals/highrise-api/blob/master/sections/data_reference.md#user

    NB this is currently READ-ONLY, and has only a single `get(id)` method.
    """

    singular = 'user'
    plural = 'users'

    def __new__(cls, extended_fields={}, **kwargs):
        # set the base fields dictionary and extend it with any additional fields
        cls.fields = {
            'id': HighriseField(type='id'),
            'name': HighriseField(type=str),
            'email_address': HighriseField(type=str),
            'created_at': HighriseField(),
            'updated_at': HighriseField(),
            'admin': HighriseField(type=bool)
        }
        cls.fields.update(extended_fields)

        # send back the object reference
        return HighriseObject.__new__(cls)

    @classmethod
    def get(cls, id):
        """Get a single user by id."""

        # retrieve the person from Highrise
        xml = Highrise.request('/{}/{}.xml'.format(cls.plural, id))

        # return a person object
        for obj_xml in xml.iter(tag=cls.singular):
            return cls.from_xml(obj_xml)


class ElevatorError(Exception):
    pass


class BadRequest(ElevatorError):
    pass


class AuthorizationRequired(ElevatorError):
    pass


class Forbidden(ElevatorError):
    pass


class NotFound(ElevatorError):
    pass


class GatewayFailure(ElevatorError):
    pass


class GatewayConnectionError(ElevatorError):
    pass


class UnexpectedResponse(ElevatorError):
    pass


class InsufficientStorage(ElevatorError):
    pass
