"""
Base API Models

"""
import json
import re
from pathlib import Path
from itertools import zip_longest
from operator import itemgetter
from collections import namedtuple
from lib.rest_api import RestAPIException
from lib.defaults import DATA_DIR


class UpdateEval:
    def __init__(self, data):
        self.is_policy = isinstance(data, list)
        # Master template updates (PUT requests) return a dict containing 'data' key. Non-master templates don't.
        self.is_master = isinstance(data, dict) and 'data' in data

        # This is to homogenize the response payload variants
        self.data = data.get('data') if self.is_master else data

    @property
    def need_reattach(self):
        return not self.is_policy and 'processId' in self.data

    @property
    def need_reactivate(self):
        return self.is_policy and len(self.data) > 0

    def templates_affected_iter(self):
        return iter(self.data.get('masterTemplatesAffected', []))

    def __str__(self):
        return json.dumps(self.data, indent=2)

    def __repr__(self):
        return json.dumps(self.data)


class ApiPath:
    """
    Groups the API path for different operations available in an API item (i.e. get, post, put, delete).
    Each field contains a str with the API path, or None if the particular operations is not supported on this item.
    """
    __slots__ = ('get', 'post', 'put', 'delete')

    def __init__(self, get, *other_ops):
        """
        :param get: URL path for get operations
        :param other_ops: URL path for post, put and delete operations, in this order. If an item is not specified
                          the same URL as the last operation provided is used.
        """
        self.get = get
        last_op = other_ops[-1] if other_ops else get
        for field, value in zip_longest(self.__slots__[1:], other_ops, fillvalue=last_op):
            setattr(self, field, value)


class ConditionalApiPath:
    def __init__(self, api_path_feature, api_path_cli):
        self.api_path_feature = api_path_feature
        self.api_path_cli = api_path_cli

    def __get__(self, instance, owner):
        # If called from class, assume its a feature template
        is_cli_template = instance is not None and instance.data.get('configType', 'template') == 'file'

        return self.api_path_cli if is_cli_template else self.api_path_feature


class ApiItem:
    """
    ApiItem represents a vManage API element defined by an ApiPath with GET, POST, PUT and DELETE paths. An instance
    of this class can be created to store the contents of that vManage API element (self.data field).
    """
    api_path = None     # An ApiPath instance
    id_tag = None
    name_tag = None

    def __init__(self, data):
        """
        :param data: dict containing the information to be associated with this api item
        """
        self.data = data

    @property
    def uuid(self):
        return self.data[self.id_tag] if self.id_tag is not None else None

    @property
    def name(self):
        return self.data[self.name_tag] if self.name_tag is not None else None

    @property
    def is_empty(self):
        return self.data is None or len(self.data) == 0

    @classmethod
    def get(cls, api, *path_entries):
        try:
            return cls.get_raise(api, *path_entries)
        except RestAPIException:
            return None

    @classmethod
    def get_raise(cls, api, *path_entries):
        return cls(api.get(cls.api_path.get, *path_entries))

    def __str__(self):
        return json.dumps(self.data, indent=2)

    def __repr__(self):
        return json.dumps(self.data)


class IndexApiItem(ApiItem):
    """
    IndexApiItem is an index-type ApiItem that can be iterated over, returning iter_fields
    """
    def __init__(self, data):
        """
        :param data: dict containing the information to be associated with this API item.
        """
        super().__init__(data.get('data') if isinstance(data, dict) else data)

    # Iter_fields should be defined in subclasses and needs to be a tuple subclass.
    iter_fields = None

    def __iter__(self):
        return self.iter(*self.iter_fields)

    def iter(self, *iter_fields):
        return (itemgetter(*iter_fields)(elem) for elem in self.data)


class ConfigItem(ApiItem):
    """
    ConfigItem is an ApiItem that can be backed up and restored
    """
    store_path = None
    store_file = None
    root_dir = DATA_DIR
    factory_default_tag = 'factoryDefault'
    readonly_tag = 'readOnly'
    owner_tag = 'owner'
    info_tag = 'infoTag'
    post_filtered_tags = None
    skip_cmp_tag_set = set()

    def __init__(self, data):
        """
        :param data: dict containing the information to be associated with this configuration item
        """
        super().__init__(data)

    def is_equal(self, other):
        local_cmp_dict = {k: v for k, v in self.data.items() if k not in self.skip_cmp_tag_set | {self.id_tag}}
        other_cmp_dict = {k: v for k, v in other.items() if k not in self.skip_cmp_tag_set | {self.id_tag}}

        return sorted(json.dumps(local_cmp_dict)) == sorted(json.dumps(other_cmp_dict))

    @property
    def is_readonly(self):
        return self.data.get(self.factory_default_tag, False) or self.data.get(self.readonly_tag, False)

    @property
    def is_system(self):
        return self.data.get(self.owner_tag, '') == 'system' or self.data.get(self.info_tag, '') == 'aci'

    @classmethod
    def get_filename(cls, ext_name, item_name, item_id):
        if item_name is None or item_id is None:
            # Assume store_file does not have variables
            return cls.store_file

        safe_name = filename_safe(item_name) if not ext_name else '{name}_{uuid}'.format(name=filename_safe(item_name),
                                                                                         uuid=item_id)
        return cls.store_file.format(item_name=safe_name, item_id=item_id)

    @classmethod
    def load(cls, node_dir, ext_name=False, item_name=None, item_id=None):
        """
        Factory method that loads data from a json file and returns a ConfigItem instance with that data

        :param node_dir: String indicating directory under root_dir used for all files from a given vManage node.
        :param ext_name: True indicates that item_names need to be extended (with item_id) in order to make their
                         filename safe version unique. False otherwise.
        :param item_name: (Optional) Name of the item being loaded. Variable used to build the filename.
        :param item_id: (Optional) UUID for the item being loaded. Variable used to build the filename.
        :return: ConfigItem object, or None if file does not exist.
        """
        dir_path = Path(cls.root_dir, node_dir, *cls.store_path)
        file_path = dir_path.joinpath(cls.get_filename(ext_name, item_name, item_id))
        try:
            with open(file_path, 'r') as read_f:
                data = json.load(read_f)
        except FileNotFoundError:
            return None
        except json.decoder.JSONDecodeError as ex:
            raise ModelException('Invalid JSON file: {file}: {msg}'.format(file=file_path, msg=ex))
        else:
            return cls(data)

    def save(self, node_dir, ext_name=False, item_name=None, item_id=None):
        """
        Save data (i.e. self.data) to a json file

        :param node_dir: String indicating directory under root_dir used for all files from a given vManage node.
        :param ext_name: True indicates that item_names need to be extended (with item_id) in order to make their
                         filename safe version unique. False otherwise.
        :param item_name: (Optional) Name of the item being saved. Variable used to build the filename.
        :param item_id: (Optional) UUID for the item being saved. Variable used to build the filename.
        :return: True indicates data has been saved. False indicates no data to save (and no file has been created).
        """
        if self.is_empty:
            return False

        dir_path = Path(self.root_dir, node_dir, *self.store_path)
        dir_path.mkdir(parents=True, exist_ok=True)

        with open(dir_path.joinpath(self.get_filename(ext_name, item_name, item_id)), 'w') as write_f:
            json.dump(self.data, write_f, indent=2)

        return True

    def post_data(self, id_mapping_dict, new_name=None):
        """
        Build payload to be used for POST requests against this config item. From self.data, perform item id
        replacements defined in id_mapping_dict, also remove item id and rename item with new_name (if provided).
        :param id_mapping_dict: {<old item id>: <new item id>} dict. Matches of <old item id> are replaced with
        <new item id>
        :param new_name: String containing new name
        :return: Dict containing payload for POST requests
        """
        # Delete keys that shouldn't be on post requests
        filtered_keys = {
            self.id_tag,
        }
        if self.post_filtered_tags is not None:
            filtered_keys.update(self.post_filtered_tags)
        post_dict = {k: v for k, v in self.data.items() if k not in filtered_keys}

        # Rename item
        if new_name is not None:
            post_dict[self.name_tag] = new_name

        return self._update_ids(id_mapping_dict, post_dict)

    def put_data(self, id_mapping_dict):
        """
        Build payload to be used for PUT requests against this config item. From self.data, perform item id
        replacements defined in id_mapping_dict.
        :param id_mapping_dict: {<old item id>: <new item id>} dict. Matches of <old item id> are replaced with
        <new item id>
        :return: Dict containing payload for PUT requests
        """
        return self._update_ids(id_mapping_dict, self.data)

    @staticmethod
    def _update_ids(id_mapping_dict, data_dict):
        def replace_id(match):
            matched_id = match.group(0)
            return id_mapping_dict.get(matched_id, matched_id)

        dict_json = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                           replace_id, json.dumps(data_dict))

        return json.loads(dict_json)

    @property
    def id_references_set(self):
        """
        Return all references to other item ids by this item
        :return: Set containing id-based references
        """
        filtered_keys = {
            self.id_tag,
        }
        filtered_data = {k: v for k, v in self.data.items() if k not in filtered_keys}

        return set(re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                              json.dumps(filtered_data)))


# Used for IndexConfigItem iter_fields when they follow (<item-id-label>, <item-name-label>) format
IdName = namedtuple('IdName', ['id', 'name'])


class IndexConfigItem(ConfigItem):
    """
    IndexConfigItem is an index-type ConfigItem that can be iterated over, returning iter_fields
    """
    def __init__(self, data):
        """
        :param data: dict containing the information to be associated with this configuration item.
        """
        super().__init__(data.get('data') if isinstance(data, dict) else data)

        # When iter_fields is a regular tuple, it is completely opaque. However, if it is an IdName, then it triggers
        # an evaluation of whether there is collision amongst the filename_safe version of all names in this index.
        # need_extended_name = True indicates that there is collision and that extended names should be used when
        # saving/loading to/from backup
        if isinstance(self.iter_fields, IdName):
            filename_safe_set = {filename_safe(item_name, lower=True) for item_name in self.iter(self.iter_fields.name)}
            self.need_extended_name = len(filename_safe_set) != len(self.data)
        else:
            self.need_extended_name = False

    # Iter_fields should be defined in subclasses and needs to be a tuple subclass.
    # When it follows the format (<item-id>, <item-name>), use an IdName namedtuple instead of regular tuple.
    iter_fields = None

    store_path = ('inventory', )

    def __iter__(self):
        return self.iter(*self.iter_fields)

    def iter(self, *iter_fields):
        return (itemgetter(*iter_fields)(elem) for elem in self.data)


def filename_safe(name, lower=False):
    """
    Perform the necessary replacements in <name> to make it filename safe.
    Any char that is not a-z, A-Z, 0-9, '_', ' ', or '-' is replaced with '_'. Convert to lowercase, if lower=True.
    :param lower: If True, apply str.lower() to result.
    :param name: name string to be converted
    :return: string containing the filename-save version of item_name
    """
    # Inspired by Django's slugify function
    cleaned = re.sub(r'[^\w\s-]', '_', name)
    return cleaned.lower() if lower else cleaned


class ModelException(Exception):
    """ Exception for REST API model errors """
    pass
