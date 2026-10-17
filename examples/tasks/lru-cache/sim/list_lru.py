class LRUCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.data = {}
        self.order = []

    def _touch(self, key):
        if key in self.order:
            self.order.remove(key)
        self.order.append(key)

    def get(self, key):
        if key not in self.data:
            return None
        self._touch(key)
        return self.data[key]

    def put(self, key, value):
        self.data[key] = value
        self._touch(key)
        if len(self.order) > self.capacity:
            del self.data[self.order.pop(0)]
