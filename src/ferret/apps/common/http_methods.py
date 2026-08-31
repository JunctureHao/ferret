"""请求方法固定词表：compose 页与断点面板共用一处口径。

下拉只提供常用方法；两处的输入框都允许自由输入（`EditableComboBox`），冷门方法
（PROPFIND 之类）手敲即可，所以词表不必穷举。
"""

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
