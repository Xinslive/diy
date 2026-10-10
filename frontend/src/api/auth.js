import { http } from './client'
export const login = (username, password) => http.post('/auth/login', { username, password }).then(r => r.data)
export const register = (payload) => http.post('/auth/register', payload).then(r => r.data)
export const me = () => http.get('/auth/me').then(r => r.data)
