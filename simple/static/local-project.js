(function initializeLocalProject(global) {
  'use strict';

  const PROJECTS_API_BASE = '/api/projects';
  const DATABASE_NAME = 'code-agent-local-projects';
  const DATABASE_VERSION = 1;
  const HANDLE_STORE_NAME = 'directory-handles';
  const POLICY_KEYS = Object.freeze([
    'max_file_size',
    'max_project_size',
    'max_project_files',
    'max_batch_files',
    'max_batch_size',
    'allowed_extensions',
    'allowed_filenames',
    'excluded_directories',
    'excluded_filenames',
    'excluded_prefixes',
    'excluded_suffixes',
  ]);
  const POLICY_NUMBER_KEYS = Object.freeze([
    'max_file_size',
    'max_project_size',
    'max_project_files',
    'max_batch_files',
    'max_batch_size',
  ]);
  const POLICY_ARRAY_KEYS = Object.freeze(POLICY_KEYS.slice(5));

  class LocalProjectError extends Error {
    constructor(message) {
      super(message);
      this.name = 'LocalProjectError';
    }
  }

  class LocalProjectLimitError extends LocalProjectError {
    constructor(message) {
      super(message);
      this.name = 'LocalProjectLimitError';
    }
  }

  function requireIdentifier(value, label) {
    if (typeof value !== 'string' || !value.trim()) {
      throw new LocalProjectError(`${label}无效`);
    }
    return value;
  }

  function cancelled(reason, message, scan) {
    const result = { status: 'cancelled', reason, message };
    if (scan) result.scan = scan;
    return result;
  }

  async function chooseDirectory() {
    if (typeof global.showDirectoryPicker !== 'function') {
      throw new LocalProjectError(
        '当前浏览器不支持本地目录授权，请使用最新版 Chrome 或 Edge 浏览器。',
      );
    }
    try {
      return await global.showDirectoryPicker({ mode: 'readwrite' });
    } catch (error) {
      if (error && error.name === 'AbortError') return null;
      throw new LocalProjectError(
        `无法打开本地目录：${error && error.message ? error.message : '授权失败'}`,
      );
    }
  }

  async function ensureReadWritePermission(directoryHandle) {
    if (!directoryHandle || typeof directoryHandle.queryPermission !== 'function') {
      throw new LocalProjectError('本地目录授权句柄无效');
    }
    const permissionOptions = { mode: 'readwrite' };
    try {
      if (await directoryHandle.queryPermission(permissionOptions) === 'granted') {
        return true;
      }
      if (typeof directoryHandle.requestPermission !== 'function') return false;
      return await directoryHandle.requestPermission(permissionOptions) === 'granted';
    } catch (_) {
      return false;
    }
  }

  function validatePolicy(policy) {
    if (!policy || typeof policy !== 'object' || Array.isArray(policy)) {
      throw new LocalProjectError('服务器上传策略无效');
    }
    const actualKeys = Object.keys(policy).sort();
    const expectedKeys = [...POLICY_KEYS].sort();
    if (
      actualKeys.length !== expectedKeys.length
      || actualKeys.some((key, index) => key !== expectedKeys[index])
    ) {
      throw new LocalProjectError('服务器上传策略字段无效');
    }
    for (const key of POLICY_NUMBER_KEYS) {
      if (!Number.isSafeInteger(policy[key]) || policy[key] <= 0) {
        throw new LocalProjectError(`服务器上传策略 ${key} 无效`);
      }
    }
    for (const key of POLICY_ARRAY_KEYS) {
      if (
        !Array.isArray(policy[key])
        || policy[key].some(value => typeof value !== 'string' || value !== value.toLowerCase())
      ) {
        throw new LocalProjectError(`服务器上传策略 ${key} 无效`);
      }
    }
    return policy;
  }

  function lowerSet(values) {
    return new Set(values.map(value => value.toLowerCase()));
  }

  function fileExtension(lowerName) {
    const dotIndex = lowerName.lastIndexOf('.');
    return dotIndex <= 0 ? '' : lowerName.slice(dotIndex);
  }

  function exclusionReason(lowerName, policySets) {
    if (
      policySets.excludedFilenames.has(lowerName)
      || policySets.excludedPrefixes.some(prefix => lowerName.startsWith(prefix))
      || policySets.excludedSuffixes.some(suffix => lowerName.endsWith(suffix))
    ) {
      return '文件名可能包含密钥或私密配置';
    }
    if (
      !policySets.allowedFilenames.has(lowerName)
      && !policySets.allowedExtensions.has(fileExtension(lowerName))
    ) {
      return '文件类型不在允许的源码或文本列表中';
    }
    return null;
  }

  function slashPath(parentPath, name) {
    return parentPath ? `${parentPath}/${name}` : name;
  }

  function pathExclusionReason(relativePath, entryName) {
    if (
      typeof relativePath !== 'string'
      || typeof entryName !== 'string'
      || relativePath.includes('\\')
      || entryName.includes('/')
      || /^[A-Za-z]:/.test(relativePath)
    ) {
      return '相对路径格式无效';
    }
    if (/[\u0000-\u001f\u007f-\u009f]/u.test(relativePath)) {
      return '相对路径包含控制字符';
    }
    const components = relativePath.split('/');
    if (
      components.length === 0
      || components.some(component => component === '' || component === '.' || component === '..')
    ) {
      return '相对路径包含无效分段';
    }
    const encoder = new global.TextEncoder();
    if (encoder.encode(relativePath).byteLength > 1024) {
      return '相对路径超过 1024 字节';
    }
    if (components.some(component => encoder.encode(component).byteLength > 255)) {
      return '相对路径分段超过 255 字节';
    }
    return null;
  }

  async function sha256Hex(arrayBuffer) {
    if (!global.crypto || !global.crypto.subtle) {
      throw new LocalProjectError('当前浏览器不支持安全的 SHA-256 文件校验');
    }
    const digest = await global.crypto.subtle.digest('SHA-256', arrayBuffer);
    return Array.from(
      new Uint8Array(digest),
      byte => byte.toString(16).padStart(2, '0'),
    ).join('');
  }

  async function scanDirectory(directoryHandle, rawPolicy) {
    const policy = validatePolicy(rawPolicy);
    if (!directoryHandle || typeof directoryHandle.values !== 'function') {
      throw new LocalProjectError('本地目录授权句柄无效');
    }
    const policySets = {
      allowedExtensions: lowerSet(policy.allowed_extensions),
      allowedFilenames: lowerSet(policy.allowed_filenames),
      excludedDirectories: lowerSet(policy.excluded_directories),
      excludedFilenames: lowerSet(policy.excluded_filenames),
      excludedPrefixes: policy.excluded_prefixes.map(value => value.toLowerCase()),
      excludedSuffixes: policy.excluded_suffixes.map(value => value.toLowerCase()),
    };
    const files = [];
    const excluded = [];
    let totalSize = 0;

    async function visit(handle, parentPath) {
      for await (const entry of handle.values()) {
        const relativePath = slashPath(parentPath, entry.name);
        const invalidPathReason = pathExclusionReason(relativePath, entry.name);
        if (invalidPathReason) {
          excluded.push({ relativePath, reason: invalidPathReason });
          continue;
        }
        const lowerName = String(entry.name).toLowerCase();
        if (entry.kind === 'directory') {
          if (policySets.excludedDirectories.has(lowerName)) {
            excluded.push({ relativePath, reason: '目录在排除列表中' });
          } else {
            await visit(entry, relativePath);
          }
          continue;
        }
        if (entry.kind !== 'file' || typeof entry.getFile !== 'function') {
          excluded.push({ relativePath, reason: '不是可读取的普通文件' });
          continue;
        }

        const unsafeReason = exclusionReason(lowerName, policySets);
        if (unsafeReason) {
          excluded.push({ relativePath, reason: unsafeReason });
          continue;
        }

        let file;
        try {
          file = await entry.getFile();
        } catch (_) {
          excluded.push({ relativePath, reason: '无法读取文件' });
          continue;
        }
        if (!file || !Number.isSafeInteger(file.size) || file.size < 0) {
          excluded.push({ relativePath, reason: '文件大小无效' });
          continue;
        }
        if (file.size > policy.max_file_size) {
          excluded.push({ relativePath, reason: '文件超过单文件大小限制' });
          continue;
        }

        let arrayBuffer;
        try {
          arrayBuffer = await file.arrayBuffer();
          const decoded = new global.TextDecoder('utf-8', { fatal: true }).decode(arrayBuffer);
          if (decoded.includes('\0')) {
            excluded.push({ relativePath, reason: '文件包含 NUL 字节，不是纯文本' });
            continue;
          }
        } catch (_) {
          excluded.push({ relativePath, reason: '文件不是有效的 UTF-8 文本' });
          continue;
        }

        const nextCount = files.length + 1;
        const nextSize = totalSize + file.size;
        if (nextCount > policy.max_project_files) {
          throw new LocalProjectLimitError('项目文件数量超过上传限制');
        }
        if (nextSize > policy.max_project_size) {
          throw new LocalProjectLimitError('项目总大小超过上传限制');
        }
        files.push({
          relativePath,
          file,
          size: file.size,
          sha256: await sha256Hex(arrayBuffer),
        });
        totalSize = nextSize;
      }
    }

    await visit(directoryHandle, '');
    return {
      files,
      excluded,
      acceptedCount: files.length,
      totalSize,
    };
  }

  function buildUploadBatches(files, rawPolicy) {
    const policy = validatePolicy(rawPolicy);
    if (!Array.isArray(files)) {
      throw new LocalProjectError('待上传文件列表无效');
    }
    const batches = [];
    let batch = [];
    let batchSize = 0;
    for (const file of files) {
      if (!file || !Number.isSafeInteger(file.size) || file.size < 0) {
        throw new LocalProjectError('待上传文件大小无效');
      }
      if (file.size > policy.max_batch_size) {
        throw new LocalProjectLimitError('单个文件超过客户端批次大小限制');
      }
      if (
        batch.length > 0
        && (
          batch.length + 1 > policy.max_batch_files
          || batchSize + file.size > policy.max_batch_size
        )
      ) {
        batches.push(batch);
        batch = [];
        batchSize = 0;
      }
      batch.push(file);
      batchSize += file.size;
    }
    if (batch.length > 0) batches.push(batch);
    return batches;
  }

  function openHandleDatabase() {
    if (!global.indexedDB || typeof global.indexedDB.open !== 'function') {
      return Promise.reject(new LocalProjectError('当前浏览器无法保存本地目录授权'));
    }
    return new Promise((resolve, reject) => {
      const request = global.indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(HANDLE_STORE_NAME)) {
          database.createObjectStore(HANDLE_STORE_NAME);
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(
        new LocalProjectError('无法打开本地目录授权存储'),
      );
    });
  }

  function handleKey(userId, projectId) {
    return `${requireIdentifier(userId, '用户')}:${requireIdentifier(projectId, '项目')}`;
  }

  async function saveDirectoryHandle(userId, projectId, directoryHandle) {
    if (!directoryHandle) throw new LocalProjectError('本地目录授权句柄无效');
    const key = handleKey(userId, projectId);
    const database = await openHandleDatabase();
    return new Promise((resolve, reject) => {
      let transaction;
      try {
        transaction = database.transaction(HANDLE_STORE_NAME, 'readwrite');
        transaction.objectStore(HANDLE_STORE_NAME).put(directoryHandle, key);
      } catch (_) {
        reject(new LocalProjectError('无法保存本地目录授权'));
        return;
      }
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(new LocalProjectError('无法保存本地目录授权'));
      transaction.onabort = () => reject(new LocalProjectError('无法保存本地目录授权'));
    });
  }

  async function getDirectoryHandle(userId, projectId) {
    const key = handleKey(userId, projectId);
    const database = await openHandleDatabase();
    return new Promise((resolve, reject) => {
      let request;
      try {
        request = database.transaction(HANDLE_STORE_NAME, 'readonly')
          .objectStore(HANDLE_STORE_NAME)
          .get(key);
      } catch (_) {
        reject(new LocalProjectError('无法读取本地目录授权'));
        return;
      }
      request.onsuccess = () => resolve(request.result || null);
      request.onerror = () => reject(new LocalProjectError('无法读取本地目录授权'));
    });
  }

  async function deleteDirectoryHandle(userId, projectId) {
    const key = handleKey(userId, projectId);
    try {
      const database = await openHandleDatabase();
      await new Promise((resolve, reject) => {
        let transaction;
        try {
          transaction = database.transaction(HANDLE_STORE_NAME, 'readwrite');
          transaction.objectStore(HANDLE_STORE_NAME).delete(key);
        } catch (_) {
          reject(new LocalProjectError('无法清除本地目录授权'));
          return;
        }
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(
          new LocalProjectError('无法清除本地目录授权'),
        );
        transaction.onabort = () => reject(
          new LocalProjectError('无法清除本地目录授权'),
        );
      });
      return { status: 'removed', removed: true };
    } catch (_) {
      return {
        status: 'failed',
        removed: false,
        message: '无法清除本地目录授权',
      };
    }
  }

  async function getProjectConnectionStatus(options = {}) {
    let userId;
    let projectId;
    try {
      userId = requireIdentifier(options.userId, '用户');
      projectId = requireIdentifier(options.projectId, '项目');
      const directoryHandle = await getDirectoryHandle(userId, projectId);
      if (!directoryHandle) {
        return { status: 'missing', directoryName: null };
      }
      if (typeof directoryHandle.queryPermission !== 'function') {
        return {
          status: 'permission_required',
          directoryName: directoryHandle.name || null,
        };
      }
      const permission = await directoryHandle.queryPermission({ mode: 'readwrite' });
      return {
        status: permission === 'granted' ? 'connected' : 'permission_required',
        directoryName: directoryHandle.name || null,
      };
    } catch (_) {
      return { status: 'failed', directoryName: null };
    }
  }

  function flowError(message, phase, project, cause, relativePath) {
    const error = new LocalProjectError(message);
    error.phase = phase;
    if (project) error.project = project;
    if (relativePath) error.relativePath = relativePath;
    if (cause) error.cause = cause;
    return error;
  }

  async function preflightHandleStorage() {
    try {
      const database = await openHandleDatabase();
      await new Promise((resolve, reject) => {
        let transaction;
        try {
          transaction = database.transaction(HANDLE_STORE_NAME, 'readonly');
          transaction.objectStore(HANDLE_STORE_NAME).get('__availability__');
        } catch (error) {
          reject(error);
          return;
        }
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error);
      });
    } catch (error) {
      throw flowError(
        '无法使用本地目录授权存储，项目尚未创建',
        'storage-preflight',
        null,
        error,
      );
    }
  }

  function formatScanSummary(scan) {
    if (!scan || !Array.isArray(scan.excluded)) {
      throw new LocalProjectError('目录扫描结果无效');
    }
    const lines = [
      `可上传文件：${scan.acceptedCount} 个`,
      `总大小：${scan.totalSize} 字节`,
      `已排除：${scan.excluded.length} 项`,
    ];
    for (const item of scan.excluded) {
      lines.push(`- ${item.relativePath}：${item.reason}`);
    }
    return lines.join('\n');
  }

  async function requestJson(url, options = {}) {
    const response = await global.fetch(url, {
      ...options,
      credentials: 'same-origin',
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch (_) {
      payload = {};
    }
    if (!response.ok) {
      throw new LocalProjectError(
        payload.detail || payload.error || `请求失败（HTTP ${response.status}）`,
      );
    }
    return payload;
  }

  function emitProgress(callback, event) {
    if (typeof callback === 'function') callback(event);
  }

  const SHA256_HEX_PATTERN = /^[0-9a-f]{64}$/;
  const CHANGE_TYPES = new Set(['created', 'modified', 'deleted']);
  const CHANGE_STATES = new Set(['pending_local_sync', 'conflict']);
  const MAX_SYNC_FILE_SIZE = 2 * 1024 * 1024;
  const MAX_SYNC_FILE_COUNT = 10000;

  function isSha256(value) {
    return typeof value === 'string' && SHA256_HEX_PATTERN.test(value);
  }

  function isCanonicalRelativePath(relativePath) {
    return !pathExclusionReason(relativePath, String(relativePath || '').split('/').pop());
  }

  function syncFailure(relativePath, phase, message) {
    const failure = { phase, message };
    if (relativePath) failure.relativePath = relativePath;
    return failure;
  }

  function syncResult(status, projectId, syncRevision, applied, conflicts, failures) {
    return {
      status,
      projectId,
      syncRevision,
      applied,
      conflicts,
      failures,
    };
  }

  function addSyncConflict(conflicts, relativePath) {
    if (!conflicts.some(conflict => conflict.relativePath === relativePath)) {
      conflicts.push({ relativePath });
    }
  }

  function validateChange(change) {
    if (!change || typeof change !== 'object' || Array.isArray(change)) {
      throw new LocalProjectError('服务器待同步变更无效');
    }
    const expectedKeys = [
      'relative_path', 'baseline_sha256', 'server_sha256',
      'size', 'sync_state', 'change_type',
    ];
    const keys = Object.keys(change).sort();
    if (
      keys.length !== expectedKeys.length
      || expectedKeys.some(key => !Object.prototype.hasOwnProperty.call(change, key))
      || !isCanonicalRelativePath(change.relative_path)
      || !CHANGE_TYPES.has(change.change_type)
      || !CHANGE_STATES.has(change.sync_state)
      || !Number.isSafeInteger(change.size)
      || change.size < 0 || change.size > MAX_SYNC_FILE_SIZE
    ) {
      throw new LocalProjectError('服务器待同步变更无效');
    }
    if (change.change_type === 'created') {
      if (change.baseline_sha256 !== null || !isSha256(change.server_sha256)) {
        throw new LocalProjectError('服务器待同步变更无效');
      }
    } else if (change.change_type === 'modified') {
      if (!isSha256(change.baseline_sha256) || !isSha256(change.server_sha256)) {
        throw new LocalProjectError('服务器待同步变更无效');
      }
    } else if (change.baseline_sha256 === null || change.server_sha256 !== null
      || !isSha256(change.baseline_sha256)) {
      throw new LocalProjectError('服务器待同步变更无效');
    }
    return change;
  }

  function validateChangesResponse(payload, projectId) {
    const expectedKeys = ['project_id', 'sync_revision', 'count', 'changes'];
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)
      || Object.keys(payload).length !== expectedKeys.length
      || expectedKeys.some(key => !Object.prototype.hasOwnProperty.call(payload, key))
      || payload.project_id !== projectId
      || !Number.isSafeInteger(payload.sync_revision) || payload.sync_revision < 0
      || !Number.isSafeInteger(payload.count) || payload.count < 0
      || payload.count > MAX_SYNC_FILE_COUNT
      || !Array.isArray(payload.changes) || payload.count !== payload.changes.length) {
      throw new LocalProjectError('服务器待同步列表无效');
    }
    const seen = new Set();
    const changes = payload.changes.map(validateChange);
    for (const change of changes) {
      if (seen.has(change.relative_path)) {
        throw new LocalProjectError('服务器待同步列表包含重复路径');
      }
      seen.add(change.relative_path);
    }
    return { syncRevision: payload.sync_revision, changes };
  }

  function isNotFoundError(error) {
    return error && error.name === 'NotFoundError';
  }

  async function findExistingFile(directoryHandle, relativePath) {
    const parts = relativePath.split('/');
    let parent = directoryHandle;
    for (const part of parts.slice(0, -1)) {
      try {
        parent = await parent.getDirectoryHandle(part);
      } catch (error) {
        if (isNotFoundError(error)) return { exists: false, parent: null };
        throw error;
      }
    }
    try {
      return {
        exists: true,
        parent,
        fileHandle: await parent.getFileHandle(parts[parts.length - 1]),
      };
    } catch (error) {
      if (isNotFoundError(error)) return { exists: false, parent };
      throw error;
    }
  }

  async function readExistingFile(directoryHandle, relativePath) {
    const found = await findExistingFile(directoryHandle, relativePath);
    if (!found.exists) return found;
    const file = await found.fileHandle.getFile();
    if (!file || !Number.isSafeInteger(file.size) || file.size < 0
      || file.size > MAX_SYNC_FILE_SIZE) {
      throw new LocalProjectError('本地文件大小超过同步校验限制');
    }
    return {
      ...found,
      hash: await sha256Hex(await file.arrayBuffer()),
    };
  }

  async function writableFileHandle(
    directoryHandle,
    relativePath,
    createParents,
    createFile,
  ) {
    const parts = relativePath.split('/');
    let parent = directoryHandle;
    for (const part of parts.slice(0, -1)) {
      parent = await parent.getDirectoryHandle(part, { create: createParents });
    }
    return {
      parent,
      fileHandle: await parent.getFileHandle(parts[parts.length - 1], { create: createFile }),
    };
  }

  function classifyLocalChange(change, current) {
    if (change.change_type === 'created') {
      if (!current.exists) return 'write';
      return current.hash === change.server_sha256 ? 'ack' : 'conflict';
    }
    if (change.change_type === 'modified') {
      if (!current.exists) return 'conflict';
      if (current.hash === change.server_sha256) return 'ack';
      return current.hash === change.baseline_sha256 ? 'write' : 'conflict';
    }
    if (!current.exists) return 'ack';
    return current.hash === change.baseline_sha256 ? 'write' : 'conflict';
  }

  async function downloadChange(projectId, change) {
    const query = new global.URLSearchParams({
      relative_path: change.relative_path,
      server_sha256: change.server_sha256,
    });
    const response = await global.fetch(
      `${PROJECTS_API_BASE}/${encodeURIComponent(projectId)}/file?${query}`,
      { credentials: 'same-origin' },
    );
    if (!response || !response.ok) {
      throw new LocalProjectError('下载服务器文件失败');
    }
    const advertisedHash = response.headers && response.headers.get('X-Content-SHA256');
    if (advertisedHash !== change.server_sha256) {
      throw new LocalProjectError('下载文件校验失败');
    }
    const body = await response.arrayBuffer();
    if (await sha256Hex(body) !== change.server_sha256) {
      throw new LocalProjectError('下载文件校验失败');
    }
    return body;
  }

  async function permissionForSync(directoryHandle, requestPermission) {
    if (!directoryHandle || typeof directoryHandle.queryPermission !== 'function') return false;
    try {
      const mode = { mode: 'readwrite' };
      if (await directoryHandle.queryPermission(mode) === 'granted') return true;
      if (!requestPermission || typeof directoryHandle.requestPermission !== 'function') return false;
      return await directoryHandle.requestPermission(mode) === 'granted';
    } catch (_) {
      return false;
    }
  }

  async function syncProjectChanges(options = {}) {
    let userId;
    let projectId;
    try {
      userId = requireIdentifier(options.userId, '用户');
      projectId = requireIdentifier(options.projectId, '项目');
    } catch (error) {
      return syncResult(
        'failed',
        options.projectId || null,
        null,
        [],
        [],
        [syncFailure(null, 'validation', error.message)],
      );
    }
    let directoryHandle = options.directoryHandle || null;
    if (!directoryHandle) {
      try {
        directoryHandle = await getDirectoryHandle(userId, projectId);
      } catch (_) {
        return syncResult('reconnect_required', projectId, null, [], [], []);
      }
    }
    if (!await permissionForSync(directoryHandle, options.requestPermission === true)) {
      return syncResult('reconnect_required', projectId, null, [], [], []);
    }

    const progress = options.onProgress;
    const applied = [];
    const conflicts = [];
    const failures = [];
    let syncRevision = null;
    emitProgress(progress, { phase: 'listing', status: 'started', projectId });
    let changes;
    try {
      const payload = await requestJson(`${PROJECTS_API_BASE}/${encodeURIComponent(projectId)}/changes`);
      const validated = validateChangesResponse(payload, projectId);
      syncRevision = validated.syncRevision;
      changes = validated.changes;
      emitProgress(progress, { phase: 'listing', status: 'completed', projectId, count: changes.length });
    } catch (error) {
      failures.push(syncFailure(null, 'list', '读取服务器待同步变更失败'));
      emitProgress(progress, { phase: 'complete', status: 'failed', projectId });
      return syncResult('failed', projectId, syncRevision, applied, conflicts, failures);
    }

    const writes = [];
    const acknowledged = [];
    for (const change of changes) {
      try {
        const current = await readExistingFile(directoryHandle, change.relative_path);
        const classification = classifyLocalChange(change, current);
        if (classification === 'write') writes.push(change);
        else if (classification === 'ack') acknowledged.push(change);
        else addSyncConflict(conflicts, change.relative_path);
      } catch (_) {
        failures.push(syncFailure(change.relative_path, 'read', '无法读取本地文件'));
      }
    }

    const downloads = new Map();
    try {
      for (const change of writes) {
        if (change.change_type === 'deleted') continue;
        emitProgress(progress, { phase: 'downloading', projectId, relativePath: change.relative_path });
        downloads.set(change.relative_path, await downloadChange(projectId, change));
      }
    } catch (error) {
      failures.push(syncFailure(null, 'download', '下载服务器文件或校验失败'));
      emitProgress(progress, { phase: 'complete', status: 'failed', projectId });
      return syncResult('failed', projectId, syncRevision, applied, conflicts, failures);
    }

    for (const change of writes) {
      try {
        emitProgress(progress, { phase: 'writing', projectId, relativePath: change.relative_path });
        const current = await readExistingFile(directoryHandle, change.relative_path);
        const classification = classifyLocalChange(change, current);
        if (classification === 'ack') {
          acknowledged.push(change);
          applied.push(change.relative_path);
          continue;
        }
        if (classification === 'conflict') {
          addSyncConflict(conflicts, change.relative_path);
          continue;
        }
        if (change.change_type === 'deleted') {
          await current.parent.removeEntry(change.relative_path.split('/').pop());
          emitProgress(progress, { phase: 'verifying', projectId, relativePath: change.relative_path });
          const verified = await findExistingFile(directoryHandle, change.relative_path);
          if (verified.exists) throw new LocalProjectError('删除后验证失败');
        } else {
          const target = await writableFileHandle(
            directoryHandle,
            change.relative_path,
            change.change_type === 'created',
            change.change_type === 'created',
          );
          const stream = await target.fileHandle.createWritable();
          try {
            await stream.write(downloads.get(change.relative_path));
            await stream.close();
          } catch (error) {
            if (stream && typeof stream.abort === 'function') {
              try { await stream.abort(); } catch (_) { /* ignore cleanup error */ }
            }
            throw error;
          }
          emitProgress(progress, { phase: 'verifying', projectId, relativePath: change.relative_path });
          const verified = await readExistingFile(directoryHandle, change.relative_path);
          if (!verified.exists || verified.hash !== change.server_sha256) {
            throw new LocalProjectError('写入后验证失败');
          }
        }
        acknowledged.push(change);
        applied.push(change.relative_path);
      } catch (_) {
        failures.push(syncFailure(change.relative_path, 'write', '本地写入或验证失败'));
      }
    }

    const finalAcknowledged = [];
    applied.length = 0;
    for (const change of acknowledged) {
      try {
        const current = await readExistingFile(directoryHandle, change.relative_path);
        if (classifyLocalChange(change, current) !== 'ack') {
          addSyncConflict(conflicts, change.relative_path);
          continue;
        }
        finalAcknowledged.push(change);
        applied.push(change.relative_path);
      } catch (_) {
        failures.push(syncFailure(
          change.relative_path,
          'verify',
          '确认前无法重新校验本地文件',
        ));
      }
    }

    if (finalAcknowledged.length > 0) {
      emitProgress(progress, { phase: 'acknowledging', status: 'started', projectId, count: finalAcknowledged.length });
      try {
        const ack = await requestJson(
          `${PROJECTS_API_BASE}/${encodeURIComponent(projectId)}/sync/ack`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ files: finalAcknowledged.map(change => ({
              relative_path: change.relative_path,
              server_sha256: change.server_sha256,
            })) }),
          },
        );
        const expectedAcked = finalAcknowledged.map(change => change.relative_path).sort();
        if (
          !ack || ack.status !== 'acknowledged' || ack.project_id !== projectId
          || !Number.isSafeInteger(ack.sync_revision)
          || ack.count !== finalAcknowledged.length || !Array.isArray(ack.acked)
          || ack.acked.length !== expectedAcked.length
          || ack.acked.slice().sort().some((path, index) => path !== expectedAcked[index])
        ) {
          throw new LocalProjectError('服务器同步确认响应无效');
        }
        syncRevision = ack.sync_revision;
        emitProgress(progress, { phase: 'acknowledging', status: 'completed', projectId });
      } catch (error) {
        failures.push(syncFailure(null, 'ack', '服务器同步确认失败，修改将自动重试'));
      }
    }
    const status = failures.length > 0 ? 'failed' : (conflicts.length > 0 ? 'conflict' : 'synced');
    emitProgress(progress, { phase: 'complete', status, projectId, appliedCount: applied.length });
    return syncResult(status, projectId, syncRevision, applied, conflicts, failures);
  }

  async function reconnectProjectDirectory(options = {}) {
    let userId;
    let projectId;
    try {
      userId = requireIdentifier(options.userId, '用户');
      projectId = requireIdentifier(options.projectId, '项目');
    } catch (_) {
      return {
        status: 'failed',
        projectId: options.projectId || null,
        directoryName: null,
        message: '本地目录重新授权失败',
      };
    }

    let directoryHandle;
    let replacingHandle = false;
    try {
      directoryHandle = await getDirectoryHandle(userId, projectId);
      if (directoryHandle) {
        if (!await permissionForSync(directoryHandle, true)) {
          return {
            status: 'reconnect_required',
            projectId,
            directoryName: directoryHandle.name || null,
          };
        }
      } else {
        replacingHandle = true;
        directoryHandle = await chooseDirectory();
        if (!directoryHandle) {
          return {
            status: 'cancelled',
            reason: 'picker_cancelled',
            projectId,
            directoryName: null,
          };
        }
        const actualName = directoryHandle.name || '';
        const expectedName = typeof options.projectName === 'string'
          ? options.projectName
          : '';
        if (expectedName && actualName !== expectedName) {
          const confirmed = typeof options.confirmNameMismatch === 'function'
            && await options.confirmNameMismatch(actualName, expectedName);
          if (!confirmed) {
            return {
              status: 'cancelled',
              reason: 'directory_name_mismatch',
              projectId,
              directoryName: actualName || null,
            };
          }
        }
        if (!await ensureReadWritePermission(directoryHandle)) {
          return {
            status: 'reconnect_required',
            projectId,
            directoryName: actualName || null,
          };
        }
      }

      if (replacingHandle) {
        await saveDirectoryHandle(userId, projectId, directoryHandle);
      }
      const result = await syncProjectChanges({
        userId,
        projectId,
        directoryHandle,
        requestPermission: true,
        onProgress: options.onProgress,
      });
      return {
        ...result,
        directoryName: directoryHandle.name || null,
      };
    } catch (_) {
      return {
        status: 'failed',
        projectId,
        directoryName: directoryHandle && directoryHandle.name
          ? directoryHandle.name
          : null,
        message: '本地目录重新授权失败',
      };
    }
  }

  async function uploadAuthorizedDirectory(options = {}) {
    const userId = requireIdentifier(options.userId, '用户');
    const sessionId = requireIdentifier(options.sessionId, '会话');
    const directoryHandle = options.directoryHandle || await chooseDirectory();
    if (!directoryHandle) {
      return cancelled('picker_cancelled', '已取消选择本地目录');
    }
    if (!await ensureReadWritePermission(directoryHandle)) {
      return cancelled('permission_denied', '未获得本地目录的读写权限');
    }

    const policy = validatePolicy(await requestJson(`${PROJECTS_API_BASE}/upload-policy`));
    emitProgress(options.onProgress, { phase: 'scan', status: 'started' });
    const scan = await scanDirectory(directoryHandle, policy);
    emitProgress(options.onProgress, {
      phase: 'scan',
      status: 'completed',
      acceptedCount: scan.acceptedCount,
      totalSize: scan.totalSize,
      excludedCount: scan.excluded.length,
    });
    const summary = formatScanSummary(scan);
    if (
      typeof options.confirmScan === 'function'
      && !await options.confirmScan(summary)
    ) {
      return cancelled(
        'confirmation_cancelled',
        '已取消上传本地项目',
        scan,
      );
    }

    await preflightHandleStorage();

    const project = await requestJson(PROJECTS_API_BASE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: directoryHandle.name }),
    });
    try {
      await saveDirectoryHandle(userId, project.id, directoryHandle);
    } catch (error) {
      throw flowError(
        '项目已创建，但保存本地目录授权失败',
        'save',
        project,
        error,
      );
    }

    const batches = buildUploadBatches(scan.files, policy);
    let completed = 0;
    for (let batchIndex = 0; batchIndex < batches.length; batchIndex += 1) {
      for (const file of batches[batchIndex]) {
        const query = new global.URLSearchParams({
          relative_path: file.relativePath,
          sha256: file.sha256,
        });
        try {
          await requestJson(
            `${PROJECTS_API_BASE}/${encodeURIComponent(project.id)}/sync/upload?${query}`,
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/octet-stream' },
              body: file.file,
            },
          );
        } catch (error) {
          throw flowError(
            `上传文件 ${file.relativePath} 失败：${error.message || '请求失败'}`,
            'upload',
            project,
            error,
            file.relativePath,
          );
        }
        completed += 1;
        emitProgress(options.onProgress, {
          phase: 'upload',
          completed,
          total: scan.files.length,
          relativePath: file.relativePath,
          batch: batchIndex + 1,
          batchCount: batches.length,
        });
      }
    }

    const manifest = scan.files.map(file => ({
      relative_path: file.relativePath,
      sha256: file.sha256,
      size: file.size,
    }));
    emitProgress(options.onProgress, {
      phase: 'finish',
      status: 'started',
      projectId: project.id,
    });
    let finishedProject;
    try {
      finishedProject = await requestJson(
        `${PROJECTS_API_BASE}/${encodeURIComponent(project.id)}/sync/finish`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ files: manifest }),
        },
      );
    } catch (error) {
      throw flowError(
        `项目文件已上传，但完成服务器同步失败：${error.message || '请求失败'}`,
        'finish',
        project,
        error,
      );
    }
    emitProgress(options.onProgress, {
      phase: 'finish',
      status: 'completed',
      projectId: project.id,
      fileCount: finishedProject.file_count,
      totalSize: finishedProject.total_size,
    });

    let openedProject;
    try {
      openedProject = await requestJson(
        `${PROJECTS_API_BASE}/${encodeURIComponent(project.id)}/open`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId }),
        },
      );
    } catch (error) {
      throw flowError(
        `项目文件已上传，但打开项目失败：${error.message || '请求失败'}`,
        'open',
        project,
        error,
      );
    }
    return { status: 'opened', project: openedProject, scan };
  }

  global.CodeAgentLocalProject = Object.freeze({
    chooseDirectory,
    deleteDirectoryHandle,
    scanDirectory,
    buildUploadBatches,
    saveDirectoryHandle,
    getDirectoryHandle,
    getProjectConnectionStatus,
    ensureReadWritePermission,
    formatScanSummary,
    syncProjectChanges,
    reconnectProjectDirectory,
    uploadAuthorizedDirectory,
  });
})(window);
