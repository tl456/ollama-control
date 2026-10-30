from ... import attachments

def read_attachment(args, scope=None):
    return attachments.read_authorized(args, scope)
