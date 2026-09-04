"""An object store for the test suite, and the runner that installs it.

`yggdrasil/settings.py` points object storage at `http://garage:3900`. That host is a
compose service: it exists for a deployment and for the dev stack, and nowhere else. A
test run therefore either cannot reach a store at all -- CI has no such service, so every
upload died at `ensure_bucket_exists()` -- or reaches the *real* one and writes test junk
into the live bucket. Neither is a store the suite can assert against.

So the suite gets its own. `InMemoryObjectStorage` answers the same surface as
`ObjectStorage` with the same failure contract, and `StorageIsolatingRunner` puts one
behind `get_object_storage()` for the duration of the run. Production code is unchanged
and unaware: it calls the same function and gets something that keeps bytes.
"""

import hashlib
import io
from typing import BinaryIO, Dict, Generator, Optional, Tuple

from django.test.runner import DiscoverRunner

from . import object_storage
from .object_storage import ObjectInfo, ObjectStorageError


class InMemoryObjectStorage:
	"""`ObjectStorage` over a dict, honouring the same contract.

	Same method signatures, same return types, and -- the part that matters for tests
	that assert on failure -- the same exceptions: a missing key raises
	`FileNotFoundError`, an invalid key raises `ObjectStorageError`. Anything the real
	class only does over the wire (bucket creation, multipart thresholds, presigning) is
	satisfied locally rather than skipped, so a caller cannot tell which one it holds.
	"""

	def __init__(self, *, bucket: str = "test-bucket", key_prefix: str = ""):
		self.bucket = bucket
		self.key_prefix = key_prefix.strip("/")
		self.objects: Dict[str, bytes] = {}
		self.content_types: Dict[str, Optional[str]] = {}
		self.metadata: Dict[str, Dict[str, str]] = {}

	def clear(self) -> None:
		self.objects.clear()
		self.content_types.clear()
		self.metadata.clear()

	# -- keys ---------------------------------------------------------------

	def normalize_key(self, key: str) -> str:
		key = (key or "").lstrip("/")
		if ".." in key.split("/"):
			raise ObjectStorageError("Invalid object key")
		if self.key_prefix:
			return f"{self.key_prefix}/{key}" if key else self.key_prefix
		return key

	def _external_key(self, key_n: str) -> str:
		if self.key_prefix and key_n.startswith(self.key_prefix + "/"):
			return key_n[len(self.key_prefix) + 1 :]
		return key_n

	def _info(self, key: str, key_n: str) -> ObjectInfo:
		body = self.objects[key_n]
		return ObjectInfo(
			key=key,
			content_length=len(body),
			content_type=self.content_types.get(key_n),
			etag=hashlib.md5(body, usedforsecurity=False).hexdigest(),
		)

	# -- reads --------------------------------------------------------------

	def head(self, key: str) -> ObjectInfo:
		key_n = self.normalize_key(key)
		if key_n not in self.objects:
			raise FileNotFoundError(key)
		return self._info(key, key_n)

	def exists(self, key: str) -> bool:
		try:
			self.head(key)
			return True
		except FileNotFoundError:
			return False

	def get(self, key: str) -> Tuple[BinaryIO, ObjectInfo]:
		key_n = self.normalize_key(key)
		if key_n not in self.objects:
			raise FileNotFoundError(key)
		return io.BytesIO(self.objects[key_n]), self._info(key, key_n)

	def get_range(self, key: str, byte_range: str) -> Tuple[BinaryIO, ObjectInfo]:
		key_n = self.normalize_key(key)
		if key_n not in self.objects:
			raise FileNotFoundError(key)
		body = self.objects[key_n]
		# "bytes=start-end"; end is inclusive and may be absent.
		spec = (byte_range or "").split("=", 1)[-1]
		start_s, _, end_s = spec.partition("-")
		start = int(start_s) if start_s else 0
		end = int(end_s) if end_s else len(body) - 1
		chunk = body[start : end + 1]
		return io.BytesIO(chunk), ObjectInfo(
			key=key,
			content_length=len(chunk),
			content_type=self.content_types.get(key_n),
			etag=hashlib.md5(body, usedforsecurity=False).hexdigest(),
		)

	def iter_bytes(
		self, key: str, *, chunk_size: int = 1024 * 1024
	) -> Generator[bytes, None, None]:
		body, _ = self.get(key)
		while True:
			chunk = body.read(chunk_size)
			if not chunk:
				break
			yield chunk

	def list_objects(self, prefix: str = "") -> Generator[ObjectInfo, None, None]:
		prefix_n = self.normalize_key(prefix) if prefix else self.key_prefix
		for key_n in sorted(self.objects):
			if prefix_n and not key_n.startswith(prefix_n):
				continue
			key = self._external_key(key_n)
			yield self._info(key, key_n)

	def list_keys(self, prefix: str) -> Generator[str, None, None]:
		prefix_n = self.normalize_key(prefix)
		for key_n in sorted(self.objects):
			if key_n.startswith(prefix_n):
				yield self._external_key(key_n)

	def presign_get(self, key: str, *, expires_seconds: int = 600) -> str:
		key_n = self.normalize_key(key)
		return f"memory://{self.bucket}/{key_n}?expires={int(expires_seconds)}"

	# -- writes -------------------------------------------------------------

	def ensure_bucket_exists(self) -> None:
		return None

	def _put(self, key: str, body: bytes, content_type, metadata) -> ObjectInfo:
		key_n = self.normalize_key(key)
		self.objects[key_n] = body
		self.content_types[key_n] = content_type
		self.metadata[key_n] = {str(k): str(v) for k, v in (metadata or {}).items()}
		return self._info(key, key_n)

	def upload_file(
		self,
		local_path: str,
		*,
		key: str,
		content_type: Optional[str] = None,
		metadata: Optional[Dict[str, str]] = None,
	) -> ObjectInfo:
		with open(local_path, "rb") as fh:
			return self._put(key, fh.read(), content_type, metadata)

	def upload_fileobj(
		self,
		fileobj: BinaryIO,
		*,
		key: str,
		content_type: Optional[str] = None,
		metadata: Optional[Dict[str, str]] = None,
	) -> ObjectInfo:
		return self._put(key, fileobj.read(), content_type, metadata)

	def delete(self, key: str) -> None:
		key_n = self.normalize_key(key)
		self.objects.pop(key_n, None)
		self.content_types.pop(key_n, None)
		self.metadata.pop(key_n, None)

	# -- cross-bucket -------------------------------------------------------
	#
	# One dict stands for every bucket, so a copy from elsewhere is a copy from here.
	# The size argument and the multipart threshold are a wire concern with nothing to
	# decide in memory; accepting the argument keeps the signature honest.

	def copy_from(
		self,
		*,
		source_bucket: str,
		source_key: str,
		dest_key: str,
		size: Optional[int] = None,
	) -> None:
		source_n = self.normalize_key(source_key)
		if source_n not in self.objects:
			raise ObjectStorageError(
				f"copy {source_bucket}/{source_key} -> {self.bucket}/{dest_key}: no such key"
			)
		self._put(
			dest_key,
			self.objects[source_n],
			self.content_types.get(source_n),
			self.metadata.get(source_n),
		)

	def head_source(self, bucket: str, key: str) -> int:
		key_n = self.normalize_key(key)
		if key_n not in self.objects:
			raise ObjectStorageError(f"head {bucket}/{key}: no such key")
		return len(self.objects[key_n])


def _result_class_clearing(store: InMemoryObjectStorage, base):
	"""`base`, but the store starts empty for every test.

	Without this the store is one shared mutable across 1,300 tests: a write in one
	makes another's "is it absent?" answer yes for the wrong reason, and the order the
	suite happens to run in decides which. Cleared at `startTest` rather than after, so
	a debugger stopped in a failing test still sees what that test wrote.
	"""

	class _ClearingResult(base):
		def startTest(self, test):
			store.clear()
			super().startTest(test)

	return _ClearingResult


class StorageIsolatingRunner(DiscoverRunner):
	"""The default test runner: swaps object storage for `InMemoryObjectStorage`.

	`get_object_storage()` memoises into a module-level singleton, and every caller in
	the codebase goes through that function, so seeding the singleton reaches all of
	them -- including modules that imported the function by name.
	"""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.storage = InMemoryObjectStorage()
		self._real_storage = None

	def setup_test_environment(self, **kwargs):
		super().setup_test_environment(**kwargs)
		self._real_storage = object_storage._storage_singleton
		object_storage._storage_singleton = self.storage

	def teardown_test_environment(self, **kwargs):
		object_storage._storage_singleton = self._real_storage
		super().teardown_test_environment(**kwargs)

	def get_test_runner_kwargs(self):
		kwargs = super().get_test_runner_kwargs()
		kwargs["resultclass"] = _result_class_clearing(
			self.storage, kwargs["resultclass"] or self.test_runner.resultclass
		)
		return kwargs
