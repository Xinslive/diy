import { http } from './client'
export const subjectTree = (bookId) => http.get(`/books/${bookId}/subjects`).then(r => r.data)
export const subjectFlat = (bookId) => http.get(`/books/${bookId}/subjects/flat`).then(r => r.data)
/** 常用科目：按这套账里已录入的分录数排 */
export const createSubject = (bookId, payload) => http.post(`/books/${bookId}/subjects`, payload).then(r => r.data)
export const updateSubject = (bookId, id, payload) => http.patch(`/books/${bookId}/subjects/${id}`, payload).then(r => r.data)
export const deleteSubject = (bookId, id) => http.delete(`/books/${bookId}/subjects/${id}`).then(r => r.data)
