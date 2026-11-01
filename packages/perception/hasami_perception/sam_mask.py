"""Python port of Meta's @meta-sam/parser 0.1.0 one_bit SAM mask codec.

Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.
Derived from dist/mask-codec.js, used under the SAM License (November 19, 2025).
The complete agreement is reproduced below and in the adjacent SAM-LICENSE file.
Redistribution of this port is subject to those terms.

SAM License
Last Updated: November 19, 2025

“Agreement” means the terms and conditions for use, reproduction, distribution and modification of the SAM Materials set forth herein.


“SAM Materials” means, collectively, Documentation and the models, software and algorithms, including machine-learning model code, trained model weights, inference-enabling code, training-enabling code, fine-tuning enabling code, and other elements of the foregoing distributed by Meta and made available under this Agreement.

“Documentation” means the specifications, manuals and documentation accompanying
SAM Materials distributed by Meta.


“Licensee” or “you” means you, or your employer or any other person or entity (if you are entering into this Agreement on such person or entity’s behalf), of the age required under applicable laws, rules or regulations to provide legal consent and that has legal authority to bind your employer or such other person or entity if you are entering in this Agreement on their behalf.


“Meta” or “we” means Meta Platforms Ireland Limited (if you are located in or, if you are an entity, your principal place of business is in the EEA or Switzerland) or Meta Platforms, Inc. (if you are located outside of the EEA or Switzerland).


“Sanctions” means any economic or trade sanctions or restrictions administered or enforced by the United States (including the Office of Foreign Assets Control of the U.S. Department of the Treasury (“OFAC”), the U.S. Department of State and the U.S. Department of Commerce), the United Nations, the European Union, or the United Kingdom.


“Trade Controls” means any of the following: Sanctions and applicable export and import controls.

By using or distributing any portion or element of the SAM Materials, you agree to be bound by this Agreement.


1. License Rights and Redistribution.


a. Grant of Rights. You are granted a non-exclusive, worldwide, non-transferable and royalty-free limited license under Meta’s intellectual property or other rights owned by Meta embodied in the SAM Materials to use, reproduce, distribute, copy, create derivative works of, and make modifications to the SAM Materials.

b. Redistribution and Use.
i. Distribution of SAM Materials, and any derivative works thereof, are subject to the terms of this Agreement. If you distribute or make the SAM Materials, or any derivative works thereof, available to a third party, you may only do so under the terms of this Agreement and you shall provide a copy of this Agreement with any such SAM Materials.


ii.  If you submit for publication the results of research you perform on, using, or otherwise in connection with SAM Materials, you must acknowledge the use of SAM Materials in your publication.


iii. Your use of the SAM Materials must comply with applicable laws and regulations, including Trade Control Laws and applicable privacy and data protection laws.
iv. Your use of the SAM Materials will not involve or encourage others to reverse engineer, decompile or discover the underlying components of the SAM Materials.
v. You are not the target of Trade Controls and your use of SAM Materials must comply with Trade Controls. You agree not to use, or permit others to use, SAM Materials for any activities subject to the International Traffic in Arms Regulations (ITAR) or end uses prohibited by Trade Controls, including those related to military or warfare purposes, nuclear industries or applications, espionage, or the development or use of guns or illegal weapons.
2. User Support. Your use of the SAM Materials is done at your own discretion; Meta does not process any information nor provide any service in relation to such use.  Meta is under no obligation to provide any support services for the SAM Materials. Any support provided is “as is”, “with all faults”, and without warranty of any kind.


3. Disclaimer of Warranty. UNLESS REQUIRED BY APPLICABLE LAW, THE SAM MATERIALS AND ANY OUTPUT AND RESULTS THEREFROM ARE PROVIDED ON AN “AS IS” BASIS, WITHOUT WARRANTIES OF ANY KIND, AND META DISCLAIMS ALL WARRANTIES OF ANY KIND, BOTH EXPRESS AND IMPLIED, INCLUDING, WITHOUT LIMITATION, ANY WARRANTIES OF TITLE, NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE. YOU ARE SOLELY RESPONSIBLE FOR DETERMINING THE APPROPRIATENESS OF USING OR REDISTRIBUTING THE SAM MATERIALS AND ASSUME ANY RISKS ASSOCIATED WITH YOUR USE OF THE SAM MATERIALS AND ANY OUTPUT AND RESULTS.

4. Limitation of Liability. IN NO EVENT WILL META OR ITS AFFILIATES BE LIABLE UNDER ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, TORT, NEGLIGENCE, PRODUCTS LIABILITY, OR OTHERWISE, ARISING OUT OF THIS AGREEMENT, FOR ANY LOST PROFITS OR ANY DIRECT OR INDIRECT, SPECIAL, CONSEQUENTIAL, INCIDENTAL, EXEMPLARY OR PUNITIVE DAMAGES, EVEN IF META OR ITS AFFILIATES HAVE BEEN ADVISED OF THE POSSIBILITY OF ANY OF THE FOREGOING.

5. Intellectual Property.


a. Subject to Meta’s ownership of SAM Materials and derivatives made by or for Meta, with respect to any derivative works and modifications of the SAM Materials that are made by you, as between you and Meta, you are and will be the owner of such derivative works and modifications.

b. If you institute litigation or other proceedings against Meta or any entity (including a cross-claim or counterclaim in a lawsuit) alleging that the SAM Materials, outputs or results, or any portion of any of the foregoing, constitutes infringement of intellectual property or other rights owned or licensable by you, then any licenses granted to you under this Agreement shall terminate as of the date such litigation or claim is filed or instituted. You will indemnify and hold harmless Meta from and against any claim by any third party arising out of or related to your use or distribution of the SAM Materials.

6. Term and Termination. The term of this Agreement will commence upon your acceptance of this Agreement or access to the SAM Materials and will continue in full force and effect until terminated in accordance with the terms and conditions herein. Meta may terminate this Agreement if you are in breach of any term or condition of this Agreement. Upon termination of this Agreement, you shall delete and cease use of the SAM Materials. Sections 3, 4 and 7 shall survive the termination of this Agreement.

7. Governing Law and Jurisdiction. This Agreement will be governed and construed under the laws of the State of California without regard to choice of law principles, and the UN Convention on Contracts for the International Sale of Goods does not apply to this Agreement. The courts of California shall have exclusive jurisdiction of any dispute arising out of this Agreement.


8. Modifications and Amendments. Meta may modify this Agreement from time to time; provided that they are similar in spirit to the current version of the Agreement, but may differ in detail to address new problems or concerns. All such changes will be effective immediately. Your continued use of the SAM Materials after any modification to this Agreement constitutes your agreement to such modification. Except as provided in this Agreement, no modification or addition to any provision of this Agreement will be binding unless it is in writing and signed by an authorized representative of both you and Meta.
"""

from __future__ import annotations

import numpy as np

_EXCLUDED = {'"', '\\', ',', ';', '<', '>', '|'}
_ALPHABET = ''.join(chr(c) for c in range(0x21, 0x7f) if chr(c) not in _EXCLUDED)
_DIGITS = {c: i for i, c in enumerate(_ALPHABET)}
_RADIX = 85
_MASK32 = 0xffffffff
_MAX_SAFE = (1 << 53) - 1
_TOP = 1 << 24
_BOTTOM = 1 << 16
_WIDTH_PADDING = 2


class InvalidSamMask(ValueError):
    """A SAM mask payload is malformed or noncanonical."""


def _digit(character: str) -> int:
    try:
        return _DIGITS[character]
    except KeyError as exc:
        raise InvalidSamMask("Mask payload contains an invalid character") from exc


def pack(data: bytes | bytearray) -> str:
    """Encode bytes with Meta's canonical base-85 framing."""
    length = len(data)
    prefix = [''] * 5
    for i in range(4, -1, -1):
        prefix[i] = _ALPHABET[length % _RADIX]
        length //= _RADIX
    if length:
        raise InvalidSamMask("Mask payload is too large")
    output = prefix
    for start in range(0, len(data), 4):
        part = data[start:start + 4]
        value = int.from_bytes(part, 'big') << (8 * (4 - len(part)))
        digits = [0] * 5
        for i in range(4, -1, -1):
            value, digits[i] = divmod(value, _RADIX)
        output.extend(_ALPHABET[d] for d in digits[:5 if len(part) == 4 else len(part) + 1])
    return ''.join(output)


def unpack(payload: str) -> bytes:
    """Decode framed base-85 and reject invalid or noncanonical representations."""
    if len(payload) < 5:
        raise InvalidSamMask("Mask payload is missing its length prefix")
    length = 0
    for character in payload[:5]:
        length = length * _RADIX + _digit(character)
    remainder = length % 4
    expected = 5 + (length // 4) * 5 + (remainder + 1 if remainder else 0)
    if len(payload) != expected:
        raise InvalidSamMask("Mask payload length does not match its prefix")
    result = bytearray()
    source = 5
    for _ in range(length // 4):
        value = 0
        for character in payload[source:source + 5]:
            value = value * _RADIX + _digit(character)
        if value > _MASK32:
            raise InvalidSamMask("Mask payload contains an out-of-range group")
        result.extend(value.to_bytes(4, 'big'))
        source += 5
    if remainder:
        value = 0
        for i in range(5):
            value = value * _RADIX + (_digit(payload[source + i]) if i < remainder + 1 else _RADIX - 1)
        if value > _MASK32:
            raise InvalidSamMask("Mask payload contains an out-of-range tail")
        result.extend(value.to_bytes(4, 'big')[:remainder])
    if pack(result) != payload:
        raise InvalidSamMask("Mask payload is not in canonical form")
    return bytes(result)


class _RangeCoder:
    def __init__(self, data: bytes | None = None):
        self.low = 0
        self.range = _MASK32
        self.data = data
        self.output = bytearray()
        self.position = 0
        self.code = 0
        self.scaled = 0
        if data is not None:
            if len(data) < 4:
                raise InvalidSamMask("Mask payload is missing its finalization")
            for _ in range(4):
                self.code = ((self.code << 8) | self._read()) & _MASK32

    def _read(self) -> int:
        if self.data is None or self.position >= len(self.data):
            raise InvalidSamMask("Mask payload ended before decoding completed")
        value = self.data[self.position]
        self.position += 1
        return value

    def _normalize(self) -> None:
        while True:
            if ((self.low ^ (self.low + self.range)) & _MASK32) < _TOP:
                pass
            elif self.range < _BOTTOM:
                self.range = (-self.low & _MASK32) & (_BOTTOM - 1)
            else:
                break
            if self.data is None:
                self.output.append((self.low >> 24) & 255)
            else:
                self.code = ((self.code << 8) | self._read()) & _MASK32
            self.low = (self.low << 8) & _MASK32
            self.range = (self.range << 8) & _MASK32

    def encode(self, cumulative: int, frequency: int, total: int) -> None:
        scaled = self.range // total
        self.low = (self.low + scaled * cumulative) & _MASK32
        self.range = scaled * frequency
        self._normalize()

    def frequency(self, total: int) -> int:
        self.scaled = self.range // total
        if not self.scaled:
            raise InvalidSamMask("Mask payload has invalid coding state")
        value = ((self.code - self.low) & _MASK32) // self.scaled
        return min(value, total - 1)

    def decode(self, cumulative: int, frequency: int) -> None:
        self.low = (self.low + self.scaled * cumulative) & _MASK32
        self.range = self.scaled * frequency
        self._normalize()

    def finish(self) -> bytes:
        for _ in range(4):
            self.output.append((self.low >> 24) & 255)
            self.low = (self.low << 8) & _MASK32
        return bytes(self.output)


def _context(padded: bytearray, p: int, prior: int, earlier: int) -> int:
    return (padded[p - 1] | padded[p - 2] << 1 |
            padded[prior - 2] << 2 | padded[prior - 1] << 3 |
            padded[prior] << 4 | padded[prior + 1] << 5 | padded[prior + 2] << 6 |
            padded[earlier - 2] << 7 | padded[earlier - 1] << 8 |
            padded[earlier] << 9 | padded[earlier + 1] << 10 | padded[earlier + 2] << 11)


def _raster_codec(raster: np.ndarray | None, payload: str | None, width: int, height: int):
    coding = raster is None
    coder = _RangeCoder(unpack(payload[1:])) if coding else _RangeCoder()
    counts = [1] * (4096 * 2)
    padded_width = width + 4
    padded = bytearray(padded_width * (height + 2))
    output = np.empty((height, width), dtype=np.uint8) if coding else None
    for y in range(height):
        row = (y + 2) * padded_width + 2
        for x in range(width):
            position = row + x
            offset = _context(padded, position, position - padded_width,
                              position - 2 * padded_width) << 1
            zero, one = counts[offset], counts[offset + 1]
            if coding:
                bit = int(coder.frequency(zero + one) >= zero)
                coder.decode(zero if bit else 0, one if bit else zero)
                output[y, x] = bit
            else:
                value = raster[y, x]
                if value != 0 and value != 1:
                    raise InvalidSamMask("Decoded mask is not binary")
                bit = int(value)
                coder.encode(zero if bit else 0, one if bit else zero, zero + one)
            padded[position] = bit
            counts[offset + bit] += 14
            if counts[offset] + counts[offset + 1] >= 4096:
                counts[offset] = (counts[offset] >> 1) or 1
                counts[offset + 1] = (counts[offset + 1] >> 1) or 1
    return output if coding else '!' + pack(coder.finish())


def encode_raster(raster: np.ndarray) -> str:
    """Encode a complete binary H×W raster as a canonical one_bit payload."""
    array = np.asarray(raster)
    if array.ndim != 2 or 0 in array.shape:
        raise InvalidSamMask("Mask raster dimensions must be positive")
    return _raster_codec(array, None, array.shape[1], array.shape[0])


def decode_raster(payload: str, width: int, height: int) -> np.ndarray:
    """Decode and canonically verify a one_bit payload; return a binary H×W raster."""
    if (not isinstance(width, int) or not isinstance(height, int) or
            width <= 0 or height <= 0 or width > _MAX_SAFE or height > _MAX_SAFE or
            width * height > _MAX_SAFE):
        raise InvalidSamMask("Mask dimensions must be positive safe integers")
    if not isinstance(payload, str):
        raise InvalidSamMask("Mask payload must be a string")
    if payload.startswith('~'):
        raise NotImplementedError("SAM lossless mask decoding is not implemented")
    if not payload.startswith('!'):
        raise InvalidSamMask("one_bit mask payloads must start with !")
    raster = _raster_codec(None, payload, width, height)
    if encode_raster(raster) != payload:
        raise InvalidSamMask("Mask payload is not canonical or has invalid finalization")
    return raster
