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
    put: async (url, data) => (await http.put(path(url), data)).data,
    delete: async url => (await http.delete(path(url))).data,
  })
}
