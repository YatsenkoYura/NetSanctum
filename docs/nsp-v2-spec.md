# NSP v2 (NSP2) — Спецификация единого самодостаточного контейнера

**Версия:** 2
**Дата:** 2026-02-22
**Статус:** системный проект (spec)

## 0. Обзор

NSP2 — бинарный контейнер для офлайн-дистрибуции пакетов модулей NetSanctum.
В отличие от NSPK (v1), который упаковывал только non-binary ресурсы и требовал
внешний `manifest.json` + отдельные медиафайлы, NSP2:

- Один `.nsp` файл содержит **все** ресурсы пакета: HTML, CSS, JS, JSON, изображения,
  **и все бинарные медиа** — аудио, видео, EPUB/CBZ.
- Встроенный манифест и индекс.
- Поддерживает 2 режима в одном формате:
  - **unsealed** — открытые данные
  - **sealed (Argon2id + HKDF-SHA256 + Chunked ChaCha20-Poly1305)** — максимальная приватность
- **Clean break:** обратная совместимость со старым форматом NSPK не нужна.

### 0.1. Гарантии

1. Открытый текст существует только в RAM при чтении; на диск может записываться
   целиком как шифртекст (sealed) или как plaintext (unsealed).
2. Range-перемотка для медиа: чанки 64 KiB, `O(1)` декодирование нужного чанка
   без чтения всего файла.
3. Дедупликация: если два URL ссылаются на одинаковые raw-байты, они разделяют
   один blob_id.

### 0.2. Термины

- **Package DEK** — случайный 32-байтный ключ контейнера.
- **KEK** — ключ из парольной фразы через Argon2id, живёт только в RAM.
- **Blob key** — подключ для конкретного блоба, выводится из DEK через HKDF.
- **Resource key** — аналогично, но через другой `info`-префикс HKDF.

---

## 1. Бинарный формат

Все целые числа — **Little-Endian** (`<` в `struct.pack`).

### 1.1. Структура файла

```
+----------------+
|   Header       |   32 байта  (смещение 0)
+----------------+
|   Data Region  |   переменно (смещение 32 .. index_offset)
+----------------+
|   Index Block  |   переменно (смещение index_offset .. keyslots_offset или EOF-72)
+----------------+
|  KeySlots      |   переменно (для unsealed отсутствует)
+----------------+
|    Footer      |   72 байта  (смещение EOF - 72)
+----------------+
```

### 1.2. Header (смещение `0`, строго `32` байта)

Struct format: `<4sHH16s8s`

| Поле | Размер | Описание |
|---|---|---|
| `magic` | 4 байта | `b"NSP2"` |
| `format_version` | `uint16` | Всегда `2` |
| `flags` | `uint16` | Битовая маска (см. 1.2.1) |
| `container_id` | 16 байт | `os.urandom(16)`, уникален для каждого `.nsp` |
| `reserved` | 8 байт | `b"\x00" * 8` |

#### 1.2.1. Битовая маска `flags`

| Бит | Значение | Описание |
|---|---|---|
| `0x0001` | `FLAG_SEALED` | Контейнер зашифрован |
| `0x0002` | `FLAG_PADDED` | Data Region дополнен случайным padding |
| остальные | `0` | Зарезервировано, при чтении игнорировать |

```python
FLAG_SEALED = 0x0001
FLAG_PADDED = 0x0002
```

### 1.3. Data Region (смещение `32` .. `index_offset`)

Сплошная конкатенация блобов `blobs[0..N]` **без каких-либо заголовков, имён файлов
или разделителей**. Все смещения и размеры хранятся только в Index.

#### 1.3.1. Сжатие (применяется ДО шифрования, поблобово)

| MIME-категория | Алгоритм | Обоснование |
|---|---|---|
| `html`, `css`, `js`, `json`, `text`, `image/svg+xml` | `zstd` | Высокая степень сжатия текстовых данных |
| `binary` (mp4/webm/mp3/flac/ogg/epub/cbz/jpg/png/webp/woff2 и т.д.) | `none` | **O(1) Range-перемотка**, сжатие бинарных медиа бессмысленно |

#### 1.3.2. Режим unsealed (`FLAG_SEALED == 0`)

Блоб пишется как есть (`stored_len == encoded_len`, `chunk_size = 0`).
Если выбран zstd, блоб сжимается; если `none` — записывается без изменений.

#### 1.3.3. Режим sealed (`FLAG_SEALED == 1`)

- `chunk_size = 65536` (64 KiB).
- Ключ блоба (32 байта):
  ```
  blob_key = HKDF-SHA256(
      ikm=DEK,
      salt=container_id,
      info=b"nsp2:blob:" + struct.pack("<Q", blob_id)
  )
  ```
- Payload блоба (после сжатия, длина `encoded_len`) режется на чанки по `chunk_size`:

  - Если `encoded_len == 0`: ровно 1 пустой чанк:
    - `chunk_index = 0`, `is_last = 1`
    - На диске: **16 байт** тега Poly1305 (nonce вычисляется детерминированно, не записывается).

  - Иначе `num_chunks = ceil(encoded_len / chunk_size)`

- Для каждого чанка `i` (`0..num_chunks-1`):
  - `is_last = 1 if i == num_chunks - 1 else 0`
  - `nonce (12 байт) = b"\x00\x00\x00\x00" + struct.pack("<Q", i)`
  - `aad (33 байта) = container_id + struct.pack("<QQB", blob_id, i, is_last)`
  - `ct_chunk = ChaCha20Poly1305(blob_key).encrypt(nonce, pt_chunk, aad)`

  На диске для каждого чанка: **ТОЛЬКО** `ct_chunk` (`plain_len + 16`).
  Nonce (12 байт) **НЕ** записывается — он вычисляется детерминированно.

#### 1.3.4. Padding (`FLAG_PADDED == 1`)

Если установлен, после последнего блоба дописать случайные байты (`os.urandom`),
выровняв длину Data Region вверх до кратности `262144` байт (256 KiB).

```python
import math
data_region_end = index_offset
if flags & FLAG_PADDED:
    target = math.ceil(data_region_end / 262144) * 262144
    padding_len = target - data_region_end
    if padding_len > 0:
        write(os.urandom(padding_len))
```

### 1.4. Index Block (смещение `index_offset`, длина `index_len`)

Plaintext Index — UTF-8 JSON, сжатый одним кадром Zstd (`zstd_index`).

#### 1.4.1. Схема JSON

```json
{
  "schema_version": 2,
  "package": {
    "id": "playlist_42",
    "title": "Title",
    "revision": 1,
    "created_at": "2026-04-06T12:00:00Z",
    "origin": "https://node.local",
    "root_url": "/music/dashboard?package_id=playlist_42",
    "module": {
      "id": "music",
      "title": "Музыка",
      "root_url": "/music/dashboard",
      "api_version": 1
    }
  },
  "runtime": {
    "embedded": true,
    "sha256": "<hex sha256 от отсортированного списка (url, blob.sha256) для layer=='runtime'>"
  },
  "blobs": [
    {
      "id": 0,
      "offset": 32,
      "stored_len": 12345,
      "encoded_len": 12345,
      "raw_len": 45000,
      "compression": "zstd",
      "chunk_size": 0,
      "sha256": "<hex sha256 от raw (несжатых и незашифрованных) байт>"
    }
  ],
  "entries": [
    {
      "url": "/static/tailwind.css",
      "blob_id": 0,
      "mime": "text/css; charset=utf-8",
      "type": "css",
      "layer": "runtime"
    }
  ]
}
```

#### 1.4.2. Поля `blobs`

| Поле | Тип | Описание |
|---|---|---|
| `id` | `int` | Уникальный идентификатор блоба (0..N-1) |
| `offset` | `int` | Смещение в Data Region (начинается с 32) |
| `stored_len` | `int` | Длина на диске (после сжатия + шифрования) |
| `encoded_len` | `int` | Длина после сжатия, до шифрования |
| `raw_len` | `int` | Длина исходных несжатых байт |
| `compression` | `string` | `"zstd"` или `"none"` |
| `chunk_size` | `int` | Размер чанка (0 для unsealed, 65536 для sealed) |
| `sha256` | `string` | SHA-256 raw байт (hex) |

#### 1.4.3. Поля `entries`

| Поле | Тип | Описание |
|---|---|---|
| `url` | `string` | URL ресурса (абсолютный, локальный) |
| `blob_id` | `int` | Ссылка на блоб |
| `mime` | `string` | MIME-тип (включая charset) |
| `type` | `string` | Тип ресурса (`html`, `css`, `js`, `json`, `image`, `binary`, `text`, `sealed`) |
| `layer` | `string` | `"runtime"` для `/static/*`, `"content"` для остальных |

#### 1.4.4. Дедупликация

Если два разных URL имеют одинаковый `raw` SHA-256, они ссылаются на один
`blob_id`. При записи проверяем хеш — если совпадает, не пишем блоб повторно,
только добавляем entry.

#### 1.4.5. Запись Index Block

- **Режим unsealed:** `index_hash_or_salt` в Footer = `SHA-256(zstd_index)`.
- **Режим sealed:**
  - Генерируется `index_salt` (32 байта) → записывается в Footer.
  - Сначала полностью формируется `Footer[0..72]` (все длины уже известны).
  - `index_key = HKDF-SHA256(
      ikm=DEK,
      salt=container_id,
      info=b"nsp2:index:" + index_salt
    )`
  - `nonce = b"\x00" * 12` (одноразовый благодаря уникальному `index_salt`)
  - `aad (104 байта) = Header[0..32] + Footer[0..72]`
  - `index_block = ChaCha20Poly1305(index_key).encrypt(nonce, zstd_index, aad)`

### 1.5. KeySlots Block (смещение `keyslots_offset`, длина `keyslots_len`)

#### 1.5.1. Для unsealed

`keyslots_offset = 0`, `keyslots_len = 0` — блок отсутствует.

#### 1.5.2. Для sealed

Несжатый UTF-8 JSON (макс. 64 KiB):

```json
{
  "version": 1,
  "slots": [
    {
      "slot_id": 0,
      "type": "argon2id",
      "kdf": {
        "version": 19,
        "m_cost_kib": 65536,
        "t_cost": 3,
        "p_cost": 1
      },
      "salt_b64": "<base64url без '=' (16 байт)>",
      "nonce_b64": "<base64url без '=' (12 байт)>",
      "wrapped_dek_b64": "<base64url без '=' (48 байт: 32B DEK + 16B tag)>"
    }
  ]
}
```

##### 1.5.2.1. KEK derivation

```
KEK (32 байта) = argon2.low_level.hash_secret_raw(
    passphrase_utf8,
    salt,
    time_cost=t_cost,
    memory_cost=m_cost_kib,
    parallelism=p_cost,
    hash_len=32,
    type=Type.ID,
    version=19
)
```

##### 1.5.2.2. Slot AAD (строго 82 байта)

```
slot_aad = Header[0..32]
         + b"nsp2:slot:argon2id"
         + struct.pack("<IIII", 19, m_cost_kib, t_cost, p_cost)
         + salt
```

**НЕ использовать JSON-байты!**

##### 1.5.2.3. Wrapping DEK

```
wrapped_dek = ChaCha20Poly1305(KEK).encrypt(nonce, DEK, slot_aad)
```

##### 1.5.2.4. Лимиты чтения (anti-DoS ДО вызова Argon2id)

| Параметр | Границы |
|---|---|
| `keyslots_len` | `<= 65536` |
| `len(slots)` | `1..=8` |
| `kdf.version` | `== 19` |
| `m_cost_kib` | `16384..=524288` |
| `t_cost` | `1..=16` |
| `p_cost` | `1..=8` |
| `len(salt)` | `== 16` |
| `len(nonce)` | `== 12` |
| `len(wrapped_dek)` | `== 48` |

### 1.6. Footer (смещение `EOF - 72`, строго `72` байта)

Struct format: `<QQQIHH32sI4s`

| Поле | Размер | Описание |
|---|---|---|
| `index_offset` | `uint64` | Смещение Index Block |
| `index_len` | `uint64` | Длина Index Block (лимит чтения ≤ 64 MiB) |
| `keyslots_offset` | `uint64` | Смещение KeySlots Block |
| `keyslots_len` | `uint32` | Длина KeySlots Block |
| `flags` | `uint16` | Копия `Header.flags` |
| `format_version` | `uint16` | Должен быть `2` |
| `index_hash_or_salt` | 32 байта | См. 1.4.5 |
| `footer_crc32` | `uint32` | `zlib.crc32(footer[0:64]) & 0xFFFFFFFF` |
| `magic` | 4 байта | `b"NSP!"` |

Общий размер: `8+8+8+4+2+2+32+4+4 = 72` байта.

---

## 2. Криптография (детали)

### 2.1. Иерархия ключей (sealed)

```
passphrase (вводит пользователь)
  → Argon2id(salt, m=65536, t=3, p=1) → KEK_32 (RAM)
  → ChaCha20(KEK).open(wrapped_dek, slot_aad) → DEK_32 (RAM)

Для каждого блоба:
  DEK_32
    → HKDF-SHA256(DEK, salt=container_id, info=b"nsp2:blob:" || blob_id) → blob_key_32

Для index:
  DEK_32
    → HKDF-SHA256(DEK, salt=container_id, info=b"nsp2:index:" || index_salt) → index_key_32
```

### 2.2. HKDF-SHA256

```python
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

blob_key = HKDF(
    algorithm=hashes.SHA256(),
    length=32,
    salt=container_id,
    info=b"nsp2:blob:" + struct.pack("<Q", blob_id)
).derive(DEK)
```

### 2.3. ChaCha20-Poly1305 (IETF)

- Nonce: 12 байт (как указано для каждого контекста)
- AAD: varies per context (см. спецификацию)
- Используем `cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305`

---

## 3. Сравнение NSPK (v1) и NSP2 (v2)

| Характеристика | NSPK (v1) | NSP2 (v2) |
|---|---|---|
| Содержит бинарные медиа | Нет | Да |
| Шифрование | Нет | Argon2id + HKDF + ChaCha20 |
| Встроенный манифест | Да (простой JSON) | Да (сжатый Zstd JSON) |
| Data Region | Сырые данные + имена | Только сырые блобы |
| Magic в конце | `>Q4s` (offset + "NSPK") | Footer 72 байта с CRC32 |
| Header | Нет | 32 байта с `b"NSP2"` |
| Сжатие | Нет | Zstd для текстовых |
| Дедупликация | Нет | Да (по SHA-256) |
| Padding | Нет | Опционально (256 KiB) |
| Range-перемотка медиа | Нет | Да (чанки 64 KiB) |

---

## 4. Пример структуры файла NSP2 (sealed)

```
0x0000: Header (32 bytes)
        NSP2 | v2 | SEALED|PADDED | container_id | 0000000000000000
0x0020: Data Region (blobs concatenated, possibly padded)
        [blob0_chunks][blob1_chunks]...[blobN_chunks][random_padding...]
0x????: Index Block (encrypted ChaCha20-Poly1305)
0x????: KeySlots Block (unencrypted JSON)
0xFFFF: Footer (72 bytes)
        index_offset | index_len | keyslots_offset | keyslots_len
        | flags | 2 | index_salt | CRC32 | NSP!
```
