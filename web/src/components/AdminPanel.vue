<script setup lang="ts">
// 管理员（decisions/0023）：部门、用户（创建、改角色 / 部门、禁用、重置密码）。禁用和重置密码会让该用户的会话立刻失效
import { message } from 'ant-design-vue'
import { onMounted, ref } from 'vue'
import { api, HttpError } from '../api/http'
import type { Department, Role, User } from '../api/types'
import { ROLE_LABEL } from '../composables/usage'

const users = ref<User[]>([])
const departments = ref<Department[]>([])
const newDept = ref('')
const draft = ref({ name: '', password: '', role: 'member' as Role, department_id: null as string | null })
const resetting = ref<User | null>(null)
const newPassword = ref('')

const roleOptions = (Object.keys(ROLE_LABEL) as Role[]).map((r) => ({ value: r, label: ROLE_LABEL[r] }))
const deptOptions = () => departments.value.map((d) => ({ value: d.id, label: d.name }))

function fail(e: unknown) {
  message.error(e instanceof HttpError ? e.describe() : String(e))
}

async function load() {
  ;[users.value, departments.value] = await Promise.all([api.adminListUsers(), api.listDepartments()])
}

async function createDept() {
  if (!newDept.value.trim()) return
  try {
    await api.adminCreateDepartment(newDept.value.trim())
    newDept.value = ''
    await load()
  } catch (e) {
    fail(e)
  }
}

async function createUser() {
  try {
    await api.adminCreateUser({ ...draft.value, name: draft.value.name.trim() })
    draft.value = { name: '', password: '', role: 'member', department_id: null }
    message.success('已创建')
    await load()
  } catch (e) {
    fail(e)
  }
}

async function update(u: User, patch: Parameters<typeof api.adminUpdateUser>[1]) {
  try {
    await api.adminUpdateUser(u.id, patch)
    await load()
  } catch (e) {
    fail(e)
  }
}

async function confirmReset() {
  if (!resetting.value) return
  await update(resetting.value, { password: newPassword.value })
  resetting.value = null
  newPassword.value = ''
}

onMounted(() => load().catch(fail))

const columns = [
  { title: '用户名', dataIndex: 'name', key: 'name' },
  { title: '角色', key: 'role', width: 150 },
  { title: '部门', key: 'department', width: 180 },
  { title: '状态', key: 'status', width: 90 },
  { title: '', key: 'actions', width: 180 },
]
</script>

<template>
  <div class="admin">
    <h3>部门</h3>
    <div class="row">
      <a-tag v-for="d in departments" :key="d.id">{{ d.name }}</a-tag>
      <a-input-search v-model:value="newDept" placeholder="新部门名称" enter-button="新建" style="width: 240px" @search="createDept" />
    </div>

    <h3>新建用户</h3>
    <div class="row">
      <a-input v-model:value="draft.name" placeholder="用户名" style="width: 160px" />
      <a-input-password v-model:value="draft.password" placeholder="初始密码（至少 8 位）" style="width: 200px" />
      <a-select v-model:value="draft.role" :options="roleOptions" style="width: 130px" />
      <a-select v-model:value="draft.department_id" :options="deptOptions()" placeholder="部门" allow-clear style="width: 160px" />
      <a-button type="primary" :disabled="!draft.name.trim() || draft.password.length < 8" @click="createUser">创建</a-button>
    </div>

    <h3>用户</h3>
    <a-table :columns="columns" :data-source="users" row-key="id" size="small" :pagination="false">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'role'">
          <a-select :value="record.role" :options="roleOptions" size="small" style="width: 120px"
                    @change="(v: unknown) => update(record, { role: v as Role })" />
        </template>
        <template v-else-if="column.key === 'department'">
          <a-select :value="record.department_id ?? undefined" :options="deptOptions()" size="small" allow-clear
                    placeholder="无" style="width: 160px"
                    @change="(v: unknown) => update(record, { department_id: (v as string | undefined) ?? null })" />
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tag :color="record.disabled_at ? 'red' : 'green'">{{ record.disabled_at ? '已禁用' : '正常' }}</a-tag>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-button size="small" type="link" @click="update(record, { disabled: !record.disabled_at })">
            {{ record.disabled_at ? '启用' : '禁用' }}
          </a-button>
          <a-button size="small" type="link" @click="resetting = record">重置密码</a-button>
        </template>
      </template>
    </a-table>

    <a-modal :open="!!resetting" :title="`重置 ${resetting?.name} 的密码`" ok-text="重置" cancel-text="取消"
             :ok-button-props="{ disabled: newPassword.length < 8 }" @ok="confirmReset" @cancel="resetting = null">
      <a-input-password v-model:value="newPassword" placeholder="新密码（至少 8 位）" />
      <div class="hint">重置后，该用户已登录的会话会立刻失效。</div>
    </a-modal>
  </div>
</template>

<style scoped>
.admin {
  padding: 20px 24px;
  overflow-y: auto;
  height: 100%;
  box-sizing: border-box;
}
.row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 16px;
}
.hint {
  margin-top: 8px;
  color: #888;
  font-size: 12px;
}
</style>
