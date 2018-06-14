import datetime as dt
from contextlib import suppress

import pymongo
from funcy import compose, take, first
from pymongo.errors import DuplicateKeyError, WriteError
from steem.account import Account
from steem.post import Post
from steem.utils import keep_in_dict
from steembase.exceptions import PostDoesNotExist
from steemdata.utils import typify, json_expand, remove_body
from toolz import pipe

from utils import strip_dot_from_keys, safe_json_metadata

from accounts import myAccounts

def get_comment(identifier):
    with suppress(PostDoesNotExist):
        return pipe(
            Post(identifier).export(),
            strip_dot_from_keys,
            safe_json_metadata
        )

def update_account(mongo, username, load_extras=True):
    """ Update Account.

    If load_extras is True, update:
     - followers, followings
     - curation stats
     - withdrawal routers, conversion requests

    """
    a = Account(username)
    if a.name in myAccounts():
        account = {
            **typify(a.export(load_extras=load_extras)),
            'account': username,
            'updatedAt': dt.datetime.utcnow(),
        }
        if type(account['json_metadata']) is dict:
            account['json_metadata'] = \
                strip_dot_from_keys(account['json_metadata'])
        if not load_extras:
            account = {'$set': account}
        try:
            mongo.Accounts.update({'name': a.name}, account, upsert=True)
        except WriteError:
            # likely an invalid profile
            account['json_metadata'] = {}
            mongo.Accounts.update({'name': a.name}, account, upsert=True)
            print("Invalidated json_metadata on %s" % a.name)


def update_account_ops(mongo, username):
    """ This method will fetch entire account history, and back-fill any missing ops. """
    if username in myAccounts():
        for event in Account(username).history():
            with suppress(DuplicateKeyError):
                transform = compose(strip_dot_from_keys, remove_body, json_expand, typify)
                mongo.AccountOperations.insert_one(transform(event))


def account_operations_index(mongo, username):
    """ Lookup AccountOperations for latest synced index. """
    start_index = 0
    # use projection to ensure covered query
    highest_index = list(
        mongo.AccountOperations.find({'account': username}, {'_id': 0, 'index': 1}).
        sort("index", pymongo.DESCENDING).limit(1)
    )
    if highest_index:
        start_index = highest_index[0].get('index', 0)

    return start_index


def update_account_ops_quick(mongo, username, batch_size=200, steemd_instance=None):
    """ Only update the latest history, limited to 1 batch of defined batch_size. """
    start_index = account_operations_index(mongo, username)

    # fetch latest records and update the db
    if username in myAccounts():
        history = \
            Account(username,
                    steemd_instance=steemd_instance).history_reverse(batch_size=batch_size)
        for event in take(batch_size, history):
            if event['index'] < start_index:
                return
            with suppress(DuplicateKeyError):
                mongo.AccountOperations.insert_one(json_expand(typify(event)))


def find_latest_item(mongo, collection_name, field_name):
    last_op = mongo.db[collection_name].find_one(
        filter={},
        projection={field_name: 1, '_id': 0},
        sort=[(field_name, pymongo.DESCENDING)],
    )
    return last_op[field_name]


def parse_operation(op):
    """ Update all relevant collections that this op impacts. """
    op_type = op['type']

    update_accounts_light = set()
    update_accounts_full = set()

    def construct_identifier():
        return '@%s/%s' % (
            op.get('author', op.get('comment_author')),
            op.get('permlink', op.get('comment_permlink')),
        )

    def account_from_auths():
        return first(op.get('required_auths', op.get('required_posting_auths')))

    if op_type in ['account_create',
                   'account_create_with_delegation']:
        update_accounts_light.add(op['creator'])
        update_accounts_full.add(op['new_account_name'])

    elif op_type in ['account_update',
                     'withdraw_vesting',
                     'claim_reward_balance',
                     'return_vesting_delegation',
                     'account_witness_vote']:
        update_accounts_light.add(op['account'])

    elif op_type == 'account_witness_proxy':
        update_accounts_light.add(op['account'])
        update_accounts_light.add(op['proxy'])

    elif op_type in ['author_reward', 'comment']:
        update_accounts_light.add(op['author'])

    elif op_type == 'vote':
        update_accounts_light.add(op['voter'])

    elif op_type == 'cancel_transfer_from_savings':
        update_accounts_light.add(op['from'])

    elif op_type == 'change_recovery_account':
        update_accounts_light.add(op['account_to_recover'])

    elif op_type == 'comment_benefactor_reward':
        update_accounts_light.add(op['benefactor'])

    elif op_type == ['convert',
                     'fill_convert_request',
                     'interest',
                     'limit_order_cancel',
                     'limit_order_create',
                     'shutdown_witness',
                     'witness_update']:
        update_accounts_light.add(op['owner'])

    elif op_type == 'curation_reward':
        update_accounts_light.add(op['curator'])

    elif op_type in ['custom', 'custom_json']:
        update_accounts_light.add(account_from_auths())

    elif op_type == 'delegate_vesting_shares':
        update_accounts_light.add(op['delegator'])
        update_accounts_light.add(op['delegatee'])

    elif op_type == 'delete_comment':
        update_accounts_light.add(op['author'])

    elif op_type in ['escrow_approve',
                     'escrow_dispute',
                     'escrow_release',
                     'escrow_transfer']:
        accs = keep_in_dict(op, ['agent', 'from', 'to', 'who', 'receiver']).values()
        update_accounts_light.update(accs)

    elif op_type == 'feed_publish':
        update_accounts_light.add(op['publisher'])

    elif op_type in ['fill_order']:
        update_accounts_light.add(op['open_owner'])
        update_accounts_light.add(op['current_owner'])

    elif op_type in ['fill_vesting_withdraw']:
        update_accounts_light.add(op['to_account'])
        update_accounts_light.add(op['from_account'])

    elif op_type == 'pow2':
        acc = op['work'][1]['input']['worker_account']
        update_accounts_light.add(acc)

    elif op_type in ['recover_account',
                     'request_account_recovery']:
        update_accounts_light.add(op['account_to_recover'])

    elif op_type == 'set_withdraw_vesting_route':
        update_accounts_light.add(op['from_account'])
        update_accounts_light.add(op['to_account'])
    elif op_type in ['transfer',
                     'transfer_from_savings',
                     'transfer_to_savings',
                     'transfer_to_vesting']:
        accs = keep_in_dict(op, ['agent', 'from', 'to', 'who', 'receiver']).values()
        update_accounts_light.update(accs)

    # # handle followers
    # if op_type == 'custom_json':
    #     with suppress(ValueError):
    #         cmd, op_json = json.loads(op['json'])  # ['follow', {data...}]
    #         if cmd == 'follow':
    #             accs = keep_in_dict(op_json, ['follower', 'following']).values()
    #             update_accounts_light.discard(first(accs))
    #             update_accounts_light.discard(second(accs))
    #             update_accounts_full.update(accs)

    return {
        'accounts': list(update_accounts_full),
        'accounts_light': list(update_accounts_light)
    }
