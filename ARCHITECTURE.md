# Architecture

## 1. Общая схема

Целевая архитектура проекта:

```text
                         Telegram
                            │
                            ▼
                    Application Layer
                            │
                            ▼
                    Business Logic
                            │
          ┌─────────────────┼─────────────────┐
          │                 │                 │
          ▼                 ▼                 ▼
      AIService      InventoryService    MarketService
          │                 │                 │
          │                 │          ┌──────┼──────┐
          │                 │          │      │      │
          │                 │          ▼      ▼      ▼
          │                 │       MSK    EKB   Склады
          │                 │
          └─────────────────┼─────────────────┘
                            │
                            ▼
                       Repositories
                            │
                            ▼
                          SQLite
```

---

## 2. Основные слои

### Telegram Layer

Отвечает только за взаимодействие с пользователем.

Задачи:

- получение сообщений;
- получение файлов;
- отправка ответов;
- обработка команд;
- передача данных в application layer.

Telegram handlers не должны содержать бизнес-логику.

---

### Application Layer

Оркестрирует выполнение пользовательского сценария.

Например:

```text
Пользователь:
"Нужна арматура 12 А500 20 тонн в Москве"
```

Application layer:

1. принимает запрос;
2. передаёт текст в AIService;
3. получает Structured Request;
4. валидирует результат;
5. запускает поиск собственного остатка;
6. рассчитывает дефицит;
7. при необходимости запускает MarketService;
8. собирает факты;
9. передаёт факты в AI для формирования ответа;
10. возвращает результат в Telegram.

---

## 3. Business Logic

Бизнес-логика не должна зависеть от Telegram.

Основные направления:

- обработка заявок;
- расчёт дефицита;
- проверка остатков;
- поиск по складам;
- поиск рыночных предложений;
- сравнение вариантов;
- подготовка данных для закупочного решения.

---

## 4. AIService

`AIService` — единая точка взаимодействия с AI.

Основные задачи:

- отправка запросов модели;
- parsing свободного текста;
- получение Structured Request;
- нормализация данных;
- формирование итогового ответа;
- обработка ошибок API;
- контроль модели и параметров;
- логирование необходимых технических данных.

AIService не хранит бизнес-логику.

AI не является источником истины.

---

## 5. InventoryService

Отвечает за собственные остатки компании.

Основные задачи:

- импорт данных из Excel;
- нормализация номенклатуры;
- поиск товара;
- определение доступного количества;
- расчёт дефицита;
- работа со складами.

Источник данных на текущем этапе:

```text
Excel
  ↓
Python import
  ↓
Validation / normalization
  ↓
SQLite
```

---

## 6. MarketService

Единый интерфейс для работы с внешним рынком.

Пример:

```text
MarketService
      │
      ├── MetalserviceMoscow
      │
      ├── MetalserviceEkb
      │
      └── MoscowWarehouses
```

Каждый источник реализуется отдельным адаптером.

Основная бизнес-логика не должна зависеть от конкретного сайта.

---

## 7. ContextService

Отвечает за дополнительный рыночный контекст.

Источники:

- пользовательские наблюдения;
- новости;
- информация о рынке;
- события;
- комментарии поставщиков;
- другие проверенные источники.

Контекст должен храниться с указанием:

- даты;
- источника;
- типа информации;
- текста;
- при необходимости уровня достоверности.

Важно различать:

```text
Факт
↓
Наблюдение
↓
Гипотеза
```

AI не должен превращать гипотезу в факт.

---

## 8. Repository Layer

Repository отвечает за работу с хранилищем.

Пример:

```text
ProductRepository
RequestRepository
InventoryRepository
WarehouseRepository
MarketPriceRepository
ContextRepository
```

Business Logic не должна напрямую зависеть от SQL-запросов.

---

## 9. Database

На текущем этапе:

```text
SQLite
```

Целевые основные сущности:

```text
Product
Request
RequestItem
Warehouse
Inventory
MarketPrice
MarketSource
ContextEvent
```

Архитектура должна позволять заменить:

```text
SQLite → PostgreSQL
```

без переписывания бизнес-логики.

---

## 10. Data Flow

### Пользовательская заявка

```text
Telegram
   ↓
Raw text
   ↓
AIService
   ↓
Structured Request
   ↓
Validation
   ↓
Business Logic
```

### Проверка собственного остатка

```text
Structured Request
   ↓
InventoryService
   ↓
InventoryRepository
   ↓
SQLite
   ↓
Available quantity
   ↓
Deficit
```

### Проверка рынка

```text
Deficit
   ↓
MarketService
   ↓
Market Source adapters
   ↓
Normalized market offers
   ↓
MarketPriceRepository
   ↓
SQLite
```

### Итоговый ответ

```text
Request
+
Inventory
+
Warehouse availability
+
Market offers
+
Market context
        ↓
    AIService
        ↓
 Procurement answer
        ↓
      Telegram
```

---

## 11. Правило источника истины

При конфликте между AI и фактическими данными:

```text
Database / Source
        >
Business Logic
        >
AI interpretation
```

AI никогда не должен подменять фактические данные.

Если данных недостаточно:

```text
"Недостаточно данных для расчёта."
```

а не предположение.

---

## 12. Первый end-to-end сценарий

Первый сценарий разработки:

```text
"Нужна арматура 12 А500 20 тонн в Москве"
```

Ожидаемый pipeline:

```text
Telegram
   ↓
AIService
   ↓
{
  product: "арматура",
  diameter: 12,
  steel: "А500",
  quantity_tons: 20,
  city: "Москва"
}
   ↓
Validation
   ↓
Product
   ↓
Inventory
   ↓
Deficit
   ↓
Market
   ↓
Context
   ↓
AIService
   ↓
Ответ закупщику
```

Этот сценарий является первым интеграционным тестом архитектуры.

---

## 13. Принципы разработки

### Single Responsibility

Каждый модуль отвечает за одну область.

### Separation of Concerns

Telegram, AI, бизнес-логика, БД и источники данных разделены.

### Replaceability

Внешние источники можно заменять без переписывания всей системы.

### Testability

Критическая бизнес-логика должна тестироваться без Telegram и без реального AI API.

### Explicit Data

Между слоями используются понятные структурированные модели.

### Minimal Complexity

Не добавлять инфраструктуру, которая не нужна текущему этапу.

---

## 14. Что не должно находиться в Telegram handlers

Запрещено помещать туда:

- SQL-запросы;
- расчёт дефицита;
- бизнес-правила закупки;
- парсинг сайтов;
- обработку Excel;
- прямую работу с AI API;
- сложную нормализацию товаров.

Handlers должны оставаться тонкими.

---

## 15. Целевое направление развития

Архитектура развивается постепенно:

```text
Telegram
   ↓
AI Parsing
   ↓
Structured Request
   ↓
SQLite
   ↓
Inventory
   ↓
Warehouses
   ↓
Market Sources
   ↓
Unified Procurement Flow
   ↓
Market Context
   ↓
Analytics
```

Каждый следующий слой добавляется только после проверки предыдущего.