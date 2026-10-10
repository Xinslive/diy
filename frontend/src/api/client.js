import axios from 'axios'
import { ElMessage } from 'element-plus'

export const http = axios.create({ baseURL: '/api', timeout: 30000 })

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('cwq_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (r) => r,
  (error) => {
    const status = error.response?.status
    if (status === 401) {
      localStorage.removeItem('cwq_token')
      if (!location.hash.includes('/login')) location.hash = '#/login'
      return Promise.reject(error)
    }
    const detail = error.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail[0]?.msg : null
    if (msg) ElMessage.error(msg)
    return Promise.reject(error)
  }
)

export const apiUrl = (path, params = {}) => {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
  ).toString()
  return `/api${path}${qs ? `?${qs}` : ''}`
}
