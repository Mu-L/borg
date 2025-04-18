import binascii
import pkgutil
import textwrap
from hashlib import sha256

from ..helpers import Error, yes, bin_to_hex, hex_to_bin, dash_open
from ..repoobj import RepoObj


from .key import CHPOKeyfileKey, RepoKeyNotFoundError, KeyBlobStorage, identify_key


class NotABorgKeyFile(Error):
    """This file is not a borg key backup, aborting."""

    exit_mcode = 43


class RepoIdMismatch(Error):
    """This key backup seems to be for a different backup repository, aborting."""

    exit_mcode = 45


class UnencryptedRepo(Error):
    """Key management not available for unencrypted repositories."""

    exit_mcode = 46


class UnknownKeyType(Error):
    """Key type {0} is unknown."""

    exit_mcode = 47


def sha256_truncated(data, num):
    h = sha256()
    h.update(data)
    return h.hexdigest()[:num]


class KeyManager:
    def __init__(self, repository):
        self.repository = repository
        self.keyblob = None
        self.keyblob_storage = None

        manifest_chunk = repository.get_manifest()
        manifest_data = RepoObj.extract_crypted_data(manifest_chunk)
        key = identify_key(manifest_data)
        self.keyblob_storage = key.STORAGE
        if self.keyblob_storage == KeyBlobStorage.NO_STORAGE:
            raise UnencryptedRepo()

    def load_keyblob(self):
        if self.keyblob_storage == KeyBlobStorage.KEYFILE:
            k = CHPOKeyfileKey(self.repository)
            target = k.find_key()
            with open(target) as fd:
                self.keyblob = "".join(fd.readlines()[1:])

        elif self.keyblob_storage == KeyBlobStorage.REPO:
            key_data = self.repository.load_key().decode()
            if not key_data:
                # if we got an empty key, it means there is no key.
                loc = self.repository._location.canonical_path()
                raise RepoKeyNotFoundError(loc) from None
            self.keyblob = key_data

    def store_keyblob(self, args):
        if self.keyblob_storage == KeyBlobStorage.KEYFILE:
            k = CHPOKeyfileKey(self.repository)
            target = k.get_existing_or_new_target(args)

            self.store_keyfile(target)
        elif self.keyblob_storage == KeyBlobStorage.REPO:
            self.repository.save_key(self.keyblob.encode("utf-8"))

    def get_keyfile_data(self):
        data = f"{CHPOKeyfileKey.FILE_ID} {bin_to_hex(self.repository.id)}\n"
        data += self.keyblob
        if not self.keyblob.endswith("\n"):
            data += "\n"
        return data

    def store_keyfile(self, target):
        with dash_open(target, "w") as fd:
            fd.write(self.get_keyfile_data())

    def export(self, path):
        if path is None:
            path = "-"

        self.store_keyfile(path)

    def export_qr(self, path):
        if path is None:
            path = "-"

        with dash_open(path, "wb") as fd:
            key_data = self.get_keyfile_data()
            html = pkgutil.get_data("borg", "paperkey.html")
            html = html.replace(b"</textarea>", key_data.encode() + b"</textarea>")
            fd.write(html)

    def export_paperkey(self, path):
        if path is None:
            path = "-"

        def grouped(s):
            ret = ""
            i = 0
            for ch in s:
                if i and i % 6 == 0:
                    ret += " "
                ret += ch
                i += 1
            return ret

        export = "To restore key use borg key import --paper /path/to/repo\n\n"

        binary = binascii.a2b_base64(self.keyblob)
        export += "BORG PAPER KEY v1\n"
        lines = (len(binary) + 17) // 18
        repoid = bin_to_hex(self.repository.id)[:18]
        complete_checksum = sha256_truncated(binary, 12)
        export += "id: {:d} / {} / {} - {}\n".format(
            lines,
            grouped(repoid),
            grouped(complete_checksum),
            sha256_truncated((str(lines) + "/" + repoid + "/" + complete_checksum).encode("ascii"), 2),
        )
        idx = 0
        while len(binary):
            idx += 1
            binline = binary[:18]
            checksum = sha256_truncated(idx.to_bytes(2, byteorder="big") + binline, 2)
            export += f"{idx:2d}: {grouped(bin_to_hex(binline))} - {checksum}\n"
            binary = binary[18:]

        with dash_open(path, "w") as fd:
            fd.write(export)

    def import_keyfile(self, args):
        file_id = CHPOKeyfileKey.FILE_ID
        first_line = file_id + " " + bin_to_hex(self.repository.id) + "\n"
        with dash_open(args.path, "r") as fd:
            file_first_line = fd.read(len(first_line))
            if file_first_line != first_line:
                if not file_first_line.startswith(file_id):
                    raise NotABorgKeyFile()
                else:
                    raise RepoIdMismatch()
            self.keyblob = fd.read()

        self.store_keyblob(args)

    def import_paperkey(self, args):
        try:
            # imported here because it has global side effects
            import readline  # noqa
        except ImportError:
            print("Note: No line editing available due to missing readline support")

        repoid = bin_to_hex(self.repository.id)[:18]
        try:
            while True:  # used for repeating on overall checksum mismatch
                # id line input
                while True:
                    idline = input("id: ").replace(" ", "")
                    if idline == "":
                        if yes("Abort import? [yN]:"):
                            raise EOFError()

                    try:
                        (data, checksum) = idline.split("-")
                    except ValueError:
                        print("each line must contain exactly one '-', try again")
                        continue
                    try:
                        (id_lines, id_repoid, id_complete_checksum) = data.split("/")
                    except ValueError:
                        print("the id line must contain exactly two '/', try again")
                        continue
                    if sha256_truncated(data.lower().encode("ascii"), 2) != checksum:
                        print("line checksum did not match, try same line again")
                        continue
                    try:
                        lines = int(id_lines)
                    except ValueError:
                        print("internal error while parsing length")

                    break

                if repoid != id_repoid:
                    raise RepoIdMismatch()

                result = b""
                idx = 1
                # body line input
                while True:
                    inline = input(f"{idx:2d}: ")
                    inline = inline.replace(" ", "")
                    if inline == "":
                        if yes("Abort import? [yN]:"):
                            raise EOFError()
                    try:
                        (data, checksum) = inline.split("-")
                    except ValueError:
                        print("each line must contain exactly one '-', try again")
                        continue
                    try:
                        part = hex_to_bin(data)
                    except ValueError as e:
                        print(f"only characters 0-9 and a-f and '-' are valid, try again [{e}]")
                        continue
                    if sha256_truncated(idx.to_bytes(2, byteorder="big") + part, 2) != checksum:
                        print(f"line checksum did not match, try line {idx} again")
                        continue
                    result += part
                    if idx == lines:
                        break
                    idx += 1

                if sha256_truncated(result, 12) != id_complete_checksum:
                    print("The overall checksum did not match, retry or enter a blank line to abort.")
                    continue

                self.keyblob = "\n".join(textwrap.wrap(binascii.b2a_base64(result).decode("ascii"))) + "\n"
                self.store_keyblob(args)
                break

        except EOFError:
            print("\n - aborted")
            return
