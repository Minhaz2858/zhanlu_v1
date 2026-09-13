# Admin Console Test Plan

## Application Overview

Zhanlu (served at `http://localhost:8088`, backend :5002) is the current build of
the platform UI: a sidebar-driven console with Business scopes (Market, Toolkit)
and Admin scopes (User Management, Agent Observability). Authentication uses a
tabbed login form: default mode is 邮箱验证码 (email-code); a 使用密码登录 (use
password) inline control switches to the password form (邮箱 + 密码 + 登录).
After login an admin lands on `/admin/users`. This suite covers the admin console
shell that the older :8080 specs do not exercise.

## Test Scenarios

### 1. Admin Console Shell

**Seed:** real UI login via the password tab (no shared storageState; the
existing `.auth/user.json` was captured against the :8080 build and its origin
state does not authenticate :8088).

#### 1.1. login-via-password-tab

**File:** `e2e/admin-console.spec.cjs` (test 1)

**Steps:**
  1. Open `/login` and switch from email-code mode to the password form
    - expect: password form fields (邮箱 / 密码) are visible
  2. Submit admin credentials (admin@zhanlu.dev / admin123)
    - expect: redirected away from `/login` to an authenticated route
  3. Land on `/admin/users`
    - expect: heading "User Management"
    - expect: users table lists seeded accounts (admin@zhanlu.dev)

#### 1.2. business-and-admin-routes-render (market, toolkit, observability)

**File:** `e2e/admin-console.spec.cjs` (test 2 — grouped in one authenticated
test because the backend rate-limits /login to 5/min/IP and per-scenario files
would burn the budget; scenarios 1.2–1.4 below)

**Steps:**
  1. Navigate to `/market`
    - expect: heading "市场" and catalog count "共 7 个智能体" (zh UI)
    - expect: a known agent card is present (CAD代理)
  2. Navigate to `/toolkit`
    - expect: heading "技能" and at least one skill card heading (ASCII ids)
  3. Navigate to `/admin/observability`
    - expect: heading "Agent Observability" (untranslated)
    - expect: at least one invocation trace link is listed
