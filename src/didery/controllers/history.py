import falcon
import arrow
try:
    import simplejson as json
except ImportError:
    import json

from ..help import helping
from .. import didering


tempDB = {}


def parseQString(req, resp, resource, params):
    req.offset = None
    req.limit = None

    if req.query_string:
        queries = req.query_string.split('&')
        for query in queries:
            key, val = qStringValidation(query)
            if key == 'offset':
                req.offset = val
            if key == 'limit':
                req.limit = val


def qStringValidation(query):
    keyval = query.split('=')

    if len(keyval) != 2:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed Query String',
                               'url query string missing value(s).')

    key = keyval[0]
    val = keyval[1]

    try:
        val = int(val)
    except ValueError as ex:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed Query String',
                               'url query string value must be a number.')

    return key, val


def basicValidation(req, resp, resource, params):
    raw = helping.parseReqBody(req)
    body = req.body

    required = ["id", "changed", "signer", "signers"]
    helping.validateRequiredFields(required, body)

    signature = req.get_header("Signature", required=True)
    sigs = helping.parseSignatureHeader(signature)
    req.signatures = sigs

    if len(sigs) == 0:
        raise falcon.HTTPError(falcon.HTTP_401,
                               'Validation Error',
                               'Empty Signature header.')

    try:
        if not isinstance(body['signers'], list):
            body['signers'] = json.loads(
                body['signers'].replace("'", '"')
            )
    except ValueError:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed "signers" Field',
                               'signers field must be a list or array.')

    if body['id'] == "":
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed "id" Field',
                               'id field cannot be empty.')

    if body['changed'] == "":
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed "changed" Field',
                               'changed field cannot be empty.')

    try:
        req.body['signer'] = int(body['signer'])
    except ValueError:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed "signer" Field',
                               'signer field must be a number.')

    try:
        dt = arrow.get(body["changed"])
    except arrow.parser.ParserError as ex:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed "changed" Field',
                               'ISO datetime could not be parsed.')

    return raw, sigs


def validatePost(req, resp, resource, params):
    """
    Validate incoming POST request and prepare
    body of request for processing.
    :param req: Request object
    """
    raw, sigs = basicValidation(req, resp, resource, params)
    body = req.body

    sig = sigs.get('signer')  # str not bytes
    if not sig:
        raise falcon.HTTPError(falcon.HTTP_401,
                               'Validation Error',
                               'Signature header missing "signer" tag and signature.')

    try:
        didkey = helping.extractDidParts(body['id'])
    except ValueError as ex:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Invalid DID',
                               str(ex))

    if len(body['signers']) < 2:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed Field',
                               'signers field must contain at least the current public key and its first pre-rotation.')

    if body['signer'] != 0:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed Field',
                               'signer field must equal 0 on creation of new rotation history.')

    index = body['signer']
    try:
        helping.validateSignedResource(sig, raw, body['signers'][index])
    except didering.ValidationError as ex:
        raise falcon.HTTPError(falcon.HTTP_401,
                               'Validation Error',
                               'Could not validate the request body and signature. {}.'.format(ex))

    # Prevent bad actors from trying to commandeer a DID before its owner posts it
    if didkey != body['signers'][0]:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed Field',
                               'The DIDs key must match the first key in the signers field.')


def validatePut(req, resp, resource, params):
    if 'did' not in params:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Validation Error',
                               'DID value missing from url.')

    raw, sigs = basicValidation(req, resp, resource, params)
    body = req.body

    signer = sigs.get('signer')  # str not bytes
    if not signer:
        raise falcon.HTTPError(falcon.HTTP_401,
                               'Validation Error',
                               'Signature header missing signature for "signer".')

    rotation = sigs.get('rotation')  # str not bytes
    if not rotation:
        raise falcon.HTTPError(falcon.HTTP_401,
                               'Validation Error',
                               'Signature header missing signature for "rotation".')

    if len(body['signers']) < 3:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Invalid Request',
                               'PUT endpoint is for rotation events. Must contain at least the original key, a '
                               'current signing key, and a pre-rotated key.')

    if body['signer'] < 1 or body['signer'] >= len(body['signers']):
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed "signer" Field',
                               '"signer" cannot reference the first or last key in the "signers" '
                               'field on PUT requests.')

    if body['signer'] + 1 == len(body['signers']):
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed "signer" Field',
                               'Missing pre rotated key in the signers field.')

    # Prevent did data from being clobbered
    if params['did'] != body['id']:
        raise falcon.HTTPError(falcon.HTTP_400,
                               'Malformed "id" Field',
                               'Url did must match id field did.')

    index = body['signer']
    try:
        helping.validateSignedResource(signer, raw, body['signers'][index])
    except didering.ValidationError as ex:
        raise falcon.HTTPError(falcon.HTTP_401,
                               'Validation Error',
                               'Could not validate the request signature for rotation field. {}.'.format(ex))

    try:
        helping.validateSignedResource(rotation, raw, body['signers'][index-1])
    except didering.ValidationError as ex:
        raise falcon.HTTPError(falcon.HTTP_401,
                               'Validation Error',
                               'Could not validate the request signature for signer field. {}.'.format(ex))


class History:
    def __init__(self, store=None):
        """
        :param store: Store
            store is reference to ioflo data store
        """
        self.store = store

    """
    For manual testing of the endpoint:
        http localhost:8000/history/did:dad:Qt27fThWoNZsa88VrTkep6H-4HA8tr54sHON1vWl6FE=
        http localhost:8000/history
    """
    @falcon.before(parseQString)
    def on_get(self, req, resp, did=None):
        """
        Handle and respond to incoming GET request.
        :param req: Request object
        :param resp: Response object
        :param did: string
            URL parameter specifying a rotation history
        """
        offset = req.offset or 0
        limit = req.limit or 10

        if offset >= len(tempDB):
            resp.body = json.dumps({}, ensure_ascii=False)
            return

        if did is not None:
            if did not in tempDB:
                raise falcon.HTTPError(falcon.HTTP_404)

            body = tempDB[did]
        else:
            values = list(tempDB.values())
            body = {
                "data": values[offset:offset+limit]
            }
            resp.append_header('X-Total-Count', len(tempDB))

        resp.body = json.dumps(body, ensure_ascii=False)


    """
    For manual testing of the endpoint:
        http POST localhost:8000/history id="did:dad:Qt27fThWoNZsa88VrTkep6H-4HA8tr54sHON1vWl6FE=" changed="2000-01-01T00:00:00+00:00" signer=2 signers="['Qt27fThWoNZsa88VrTkep6H-4HA8tr54sHON1vWl6FE=', 'Xq5YqaL6L48pf0fu7IUhL0JRaU2_RxFP0AL43wYn148=', 'dZ74MLZXD-1QHoa73w9pQ9GroAvxqFi2RTZWlkC0raY=', '3syVH2woCpOvPF0SD9Z0bu_OxNe2ZgxKjTQ961LlMnA=']"
    """
    @falcon.before(validatePost)
    def on_post(self, req, resp):
        """
        Handle and respond to incoming POST request.
        :param req: Request object
        :param resp: Response object
        """
        result_json = req.body
        sigs = req.signatures

        # TODO uncomment code below once lmdb is implemented
        # if result_json['id'] in tempDB:
        #     raise falcon.HTTPError(falcon.HTTP_400,
        #                            'Resource Already Exists',
        #                            'Resource with did "{}" already exists. Use PUT request.'.format(result_json['id']))

        response_json = {
            "history": result_json,
            "signatures": sigs
        }

        # TODO: review signature validation for any holes
        tempDB[result_json['id']] = response_json

        resp.body = json.dumps(response_json, ensure_ascii=False)
        resp.status = falcon.HTTP_201

    """
    For manual testing of the endpoint:
        http PUT localhost:8000/history/did:dad:Qt27fThWoNZsa88VrTkep6H-4HA8tr54sHON1vWl6FE= id="did:dad:Qt27fThWoNZsa88VrTkep6H-4HA8tr54sHON1vWl6FE=" changed="2000-01-01T00:00:00+00:00" signer=2 signers="['Qt27fThWoNZsa88VrTkep6H-4HA8tr54sHON1vWl6FE=', 'Xq5YqaL6L48pf0fu7IUhL0JRaU2_RxFP0AL43wYn148=', 'dZ74MLZXD-1QHoa73w9pQ9GroAvxqFi2RTZWlkC0raY=', '3syVH2woCpOvPF0SD9Z0bu_OxNe2ZgxKjTQ961LlMnA=']"
    """
    @falcon.before(validatePut)
    def on_put(self, req, resp, did):
        """
                Handle and respond to incoming PUT request.
                :param req: Request object
                :param resp: Response object
                :param did: decentralized identifier
                """
        result_json = req.body
        sigs = req.signatures

        if did not in tempDB:
            raise falcon.HTTPError(falcon.HTTP_404)

        resource = tempDB[did]

        # TODO make sure time in changed field is greater than existing changed field
        cdt = arrow.get(resource['history']['changed'])
        udt = arrow.get(result_json['changed'])

        if cdt >= udt:
            raise falcon.HTTPError(falcon.HTTP_400,
                                   'Invalid "changed" Field',
                                   '"changed" field not later than previous update.')

        # TODO validate that previously rotated keys are not changed with this request
        current = resource['history']['signers']
        update = result_json['signers']

        if len(update) <= len(current):
            raise falcon.HTTPError(falcon.HTTP_400,
                                   'Malformed "signers" Field',
                                   'signers field is missing keys.')

        for key, val in enumerate(current):
            if update[key] != val:
                raise falcon.HTTPError(falcon.HTTP_400,
                                       'Malformed "signers" Field',
                                       'signers field missing previously verified keys.')

        response_json = {
            "history": result_json,
            "signatures": sigs
        }

        # TODO: review signature validation for any holes
        tempDB[result_json['id']] = response_json

        resp.body = json.dumps(response_json, ensure_ascii=False)
