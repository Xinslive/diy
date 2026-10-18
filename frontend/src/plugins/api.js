import { http } from '@/api/client'

export const fetchManifest = async () => (await http.get('/plugins/manifest')).data
export const listPlugins = async () => (await http.get('/system/plugins')).data
export const listEligibleUsers = async (query = '', page = 1) => (await http.get('/system/plugins/eligible-users', { params: { query, page } })).data
export const listPluginUsers = async id => (await http.get(`/system/plugins/${id}/users`)).data
export const savePluginUsers = async (id, user_ids) => (await http.put(`/system/plugins/${id}/users`, { user_ids })).data
export const pluginTables = async id => (await http.get(`/system/plugins/${id}/tables`)).data
export const pluginAction = async (id, action) => (await http.post(`/system/plugins/${id}/${action}`)).data
export const removePlugin = async id => (await http.delete(`/system/plugins/${id}`)).data
export const cleanupPlugin = async id => (await http.post(`/system/plugins/${id}/tables/cleanup`, { confirmation: id })).data
export async function uploadPlugin(file, id = null) {
  const data = new FormData()
  data.append('file', file)
  return (await http.post(id ? `/system/plugins/${id}/upgrade` : '/system/plugins/install', data, { timeout: 120000 })).data
}

export function pluginClient(id) {
  const base = `/plugins/${id}`
  const path = value => {
    if (typeof value !== 'string' || !/^\/[a-zA-Z0-9_/-]*$/.test(value) || value.includes('//')) throw new Error('插件 API 路径无效')
    return base + value
  }
  return Object.freeze({
    get: async (url, params) => (await http.get(path(url), { params })).data,
    post: async (url, data) => (await http.post(path(url), data)).data,
    patch: async (url, data) => (await http.patch(path(url), data)).data,
    put: async (url, data, params) => (await http.put(path(url), data, { params })).data,
    delete: async url => (await http.delete(path(url))).data,
    upload: async (url, files, params = {}) => {
      const data = new FormData()
      for (const file of Array.isArray(files) ? files : [files]) data.append('files', file)
      return (await http.post(path(url), data, { params, timeout: 120000 })).data
    },
    download: async url => (await http.get(path(url), { responseType: 'blob' })).data,
  })
}

// 标准服务只走当前插件命名空间，服务端仍逐次检查 capability、用户和账套角色。
export function pluginServices(api) {
  const imports = '/_host/imports'
  return Object.freeze({
    books: () => api.get('/_host/books'),
    config: Object.freeze({
      get: (table, key, bookId) => api.get(`/_host/config/${table}/${key}`, { book_id: bookId }),
      put: (table, key, bookId, expectedVersion, value) => api.put(`/_host/config/${table}/${key}`, { expected_version: expectedVersion, value }, { book_id: bookId }),
    }),
    imports: Object.freeze({
      limits: () => api.get('/_host/import-limits'),
      origin: voucherId => api.get(`/_host/vouchers/${voucherId}/origin`),
      upload: (bookId, files) => api.upload(imports, files, { book_id: bookId }),
      list: (bookId, params = {}) => api.get(imports, { ...params, book_id: bookId }),
      read: id => api.get(`${imports}/${id}`),
      edit: (id, revision, vouchers) => api.put(`${imports}/${id}/preview`, { revision, vouchers }),
      confirm: (id, revision) => api.post(`${imports}/${id}/confirm`, { revision }),
      submit: (id, revision) => api.post(`${imports}/${id}/submit`, { revision }),
      cancel: (id, revision) => api.post(`${imports}/${id}/cancel`, { revision }),
      download: (id, fileId) => api.download(`${imports}/${id}/files/${fileId}`),
    }),
  })
}
