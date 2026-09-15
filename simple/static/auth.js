(function initializeAuthentication(global) {
  'use strict';

  const AUTH_API_BASE = '/api/auth';
  const CSRF_HEADER_NAME = 'X-CSRF-Token';
  const nativeFetch = global.fetch.bind(global);
  let protectedFetchInstalled = false;

  class AuthRequestError extends Error {
    constructor(message, status) {
      super(message);
      this.name = 'AuthRequestError';
      this.status = status;
    }
  }

  async function authRequest(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const response = await nativeFetch(`${AUTH_API_BASE}${path}`, {
      ...options,
      headers,
      credentials: 'same-origin',
    });

    let payload = {};
    try {
      payload = await response.json();
    } catch (_) {
      payload = {};
    }

    if (!response.ok) {
      throw new AuthRequestError(
        payload.detail || payload.error || `请求失败（HTTP ${response.status}）`,
        response.status,
      );
    }
    return payload;
  }

  function sendCredentials(path, username, password) {
    return authRequest(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
  }

  function login(username, password) {
    return sendCredentials('/login', username, password);
  }

  function register(username, password) {
    return sendCredentials('/register', username, password);
  }

  function getCurrentAuth() {
    return authRequest('/me');
  }

  function logout(csrfToken) {
    return authRequest('/logout', {
      method: 'POST',
      headers: { [CSRF_HEADER_NAME]: csrfToken },
    });
  }

  function installProtectedFetch(csrfToken) {
    if (protectedFetchInstalled) return;
    protectedFetchInstalled = true;
    global.fetch = (input, options = {}) => {
      const isUrlObject = (
        typeof global.URL === 'function' && input instanceof global.URL
      );
      const inputUrl = typeof input === 'string'
        ? input
        : (isUrlObject ? input.href : input.url);
      let parsedUrl;
      try {
        parsedUrl = new URL(inputUrl, global.location.origin);
      } catch (_) {
        return nativeFetch(input, options);
      }

      const inputMethod = (typeof input === 'string' || isUrlObject)
        ? 'GET'
        : input.method;
      const method = (options.method || inputMethod || 'GET').toUpperCase();
      const isMutation = !['GET', 'HEAD', 'OPTIONS'].includes(method);
      const isProtectedApi = (
        parsedUrl.origin === global.location.origin
        && parsedUrl.pathname.startsWith('/api/')
      );
      if (!isMutation || !isProtectedApi) {
        return nativeFetch(input, options);
      }

      const inputHeaders = (typeof input === 'string' || isUrlObject)
        ? undefined
        : input.headers;
      const headers = new Headers(options.headers || inputHeaders || {});
      headers.set(CSRF_HEADER_NAME, csrfToken);
      return nativeFetch(input, {
        ...options,
        credentials: options.credentials || 'same-origin',
        headers,
      });
    };
  }

  const auth = {
    AUTH_API_BASE,
    AuthRequestError,
    getCurrentAuth,
    login,
    logout,
    register,
    current: null,
    ready: Promise.resolve(null),
  };
  global.CodeAgentAuth = auth;

  if (typeof document === 'undefined') return;

  function whenDocumentReady(callback) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', callback, { once: true });
    } else {
      callback();
    }
  }

  function safeNextPath() {
    const params = new URLSearchParams(global.location.search);
    const next = params.get('next');
    if (!next) return '/';
    try {
      const target = new URL(next, global.location.origin);
      if (target.origin !== global.location.origin) return '/';
      return `${target.pathname}${target.search}${target.hash}`;
    } catch (_) {
      return '/';
    }
  }

  function redirectToLogin() {
    const next = `${global.location.pathname}${global.location.search}`;
    global.location.replace(`/login?next=${encodeURIComponent(next)}`);
  }

  function showMessage(element, message, type = 'error') {
    if (!element) return;
    element.textContent = message;
    element.dataset.type = type;
    element.hidden = !message;
  }

  function setupLoginPage() {
    const form = document.getElementById('auth-form');
    const loginTab = document.getElementById('login-tab');
    const registerTab = document.getElementById('register-tab');
    const username = document.getElementById('username');
    const password = document.getElementById('password');
    const passwordConfirm = document.getElementById('password-confirm');
    const confirmField = document.getElementById('password-confirm-field');
    const submitButton = document.getElementById('auth-submit');
    const formTitle = document.getElementById('auth-form-title');
    const message = document.getElementById('auth-message');
    if (!form || !loginTab || !registerTab || !submitButton) return;

    let mode = 'login';
    const setMode = (nextMode) => {
      mode = nextMode;
      const isRegister = mode === 'register';
      loginTab.classList.toggle('active', !isRegister);
      registerTab.classList.toggle('active', isRegister);
      loginTab.setAttribute('aria-selected', String(!isRegister));
      registerTab.setAttribute('aria-selected', String(isRegister));
      confirmField.hidden = !isRegister;
      passwordConfirm.required = isRegister;
      formTitle.textContent = isRegister ? '创建你的账号' : '欢迎回来';
      submitButton.textContent = isRegister ? '注册并进入' : '登录';
      showMessage(message, '');
      username.focus();
    };

    loginTab.addEventListener('click', () => setMode('login'));
    registerTab.addEventListener('click', () => setMode('register'));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      showMessage(message, '');
      if (mode === 'register' && password.value !== passwordConfirm.value) {
        showMessage(message, '两次输入的密码不一致');
        passwordConfirm.focus();
        return;
      }

      submitButton.disabled = true;
      submitButton.textContent = mode === 'register' ? '正在注册…' : '正在登录…';
      try {
        const result = mode === 'register'
          ? await register(username.value, password.value)
          : await login(username.value, password.value);
        auth.current = result;
        showMessage(message, mode === 'register' ? '注册成功，正在进入…' : '登录成功，正在进入…', 'success');
        global.location.replace(safeNextPath());
      } catch (error) {
        showMessage(message, error.message || '操作失败，请稍后重试');
        submitButton.disabled = false;
        submitButton.textContent = mode === 'register' ? '注册并进入' : '登录';
      }
    });

    setMode('login');
  }

  function setupApplicationUser(session) {
    const username = document.getElementById('current-username');
    const logoutButton = document.getElementById('logout-btn');
    const authMessage = document.getElementById('app-auth-message');
    if (username) username.textContent = session.user.username;
    document.documentElement.classList.remove('auth-pending');

    if (!logoutButton) return;
    logoutButton.addEventListener('click', async () => {
      logoutButton.disabled = true;
      if (authMessage) authMessage.textContent = '正在退出…';
      try {
        await logout(session.csrf_token);
        global.location.replace('/login');
      } catch (error) {
        if (error.status === 401) {
          global.location.replace('/login');
          return;
        }
        if (authMessage) authMessage.textContent = error.message || '退出失败';
        logoutButton.disabled = false;
      }
    });
  }

  const page = document.documentElement.dataset.authPage;
  if (page === 'login') {
    whenDocumentReady(setupLoginPage);
    auth.ready = getCurrentAuth()
      .then((session) => {
        auth.current = session;
        global.location.replace(safeNextPath());
        return session;
      })
      .catch((error) => {
        if (error.status !== 401) {
          whenDocumentReady(() => {
            showMessage(document.getElementById('auth-message'), error.message || '认证服务暂时不可用');
          });
        }
        return null;
      });
  } else if (page === 'app') {
    auth.ready = getCurrentAuth()
      .then((session) => {
        auth.current = session;
        installProtectedFetch(session.csrf_token);
        whenDocumentReady(() => setupApplicationUser(session));
        return session;
      })
      .catch((error) => {
        document.documentElement.classList.remove('auth-pending');
        if (error.status === 401) {
          redirectToLogin();
        } else {
          whenDocumentReady(() => {
            const message = document.getElementById('app-auth-message');
            if (message) message.textContent = error.message || '无法确认登录状态';
          });
        }
        return null;
      });
  }
})(window);
