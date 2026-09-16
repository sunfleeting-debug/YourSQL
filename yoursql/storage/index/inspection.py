"""兼容旧导入路径；单页索引检查的实现已移至 ``page_inspection``。"""

from yoursql.storage.index.page_inspection import index_page_info

__all__ = ["index_page_info"]
