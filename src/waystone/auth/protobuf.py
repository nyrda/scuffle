"""Minimal protobuf encoder and bounded reader for the recovered BGS messages."""


def varint(number):
    result = bytearray()
    while number > 127:
        result.append((number & 127) | 128)
        number >>= 7
    result.append(number)
    return bytes(result)


def integer(number, value):
    return varint(number << 3) + varint(value)


def blob(number, value):
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


def read_varint(data, offset):
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('truncated varint')
        value = data[offset]
        offset += 1
        result |= (value & 127) << shift
        if value < 128:
            return result, offset
    raise ValueError('varint overflow')


def fields(data):
    offset = 0
    result = []
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError('zero field number')
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire == 2:
            size, offset = read_varint(data, offset)
            value = data[offset:offset + size]
            if len(value) != size:
                raise ValueError('truncated bytes')
            offset += size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value = data[offset:offset + size]
            if len(value) != size:
                raise ValueError('truncated fixed-width field')
            offset += size
        else:
            raise ValueError('unsupported wire type')
        result.append((number, wire, value))
    return result


def first(items, number, default=None):
    return next((v for n, _, v in items if n == number), default)
