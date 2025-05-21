This project is a Discord bot designed to enhance user interaction through integration with various AI services. It leverages OpenAI for generating contextually relevant chat responses and potentially other advanced language model tasks. Perplexity AI is integrated to provide concise and accurate answers to user queries, likely focusing on information retrieval and summarization. Google Gemini is utilized for its multimodal capabilities, allowing the bot to understand and generate responses based on a wider range of inputs, possibly including text and images.

To personalize user experiences and maintain conversational context, the bot employs a PostgreSQL database. This database stores user-specific memory, such as conversation summaries and history, enabling more coherent and tailored interactions over time. Additionally, the database is used for tracking the usage of different features, which can be valuable for understanding user preferences and optimizing the bot's performance.

## User Commands Documentation

### `!推理 <內容>`
*   **Purpose:** Performs in-depth reasoning, analysis, or generation based on the provided content. It is intended for complex queries that require significant contextual understanding and detailed responses.
*   **Input:** `<內容>` - Text content provided by the user. This can also include attached PDF files or images, which will be processed and included in the context for the AI.
*   **Actions:**
    *   Processes the input text and any attached PDF or image files. PDFs are parsed for text content, and images are processed for multimodal understanding (likely using Google Gemini).
    *   Calls a powerful language model, prioritizing OpenAI's GPT-4 Turbo (`gpt-4-turbo-preview`) for its advanced reasoning capabilities. If the input is too large for GPT-4 Turbo, it attempts to use Google Gemini Pro (`gemini-pro`) if image data is present, or falls back to OpenAI's GPT-3.5 Turbo (`gpt-3.5-turbo-1106`) for text-only inputs.
    *   Retrieves relevant past conversation snippets from the user's memory (PostgreSQL database) to provide context to the AI.
    *   Generates a response based on the input and contextual memory.
    *   Stores the new interaction (user input and AI response) in the user's memory.
    *   Tracks the number of tokens used for the OpenAI API call and updates the user's usage statistics in the database.
*   **Specific Behaviors:**
    *   **PDF/Image Processing:** Can extract text from PDF files and understand content from image files.
    *   **Token Tracking:** Monitors and records token usage for OpenAI models.
    *   **Model Switching:** Dynamically switches between AI models (GPT-4 Turbo, Gemini Pro, GPT-3.5 Turbo) based on input size, presence of images, and potential token limits.
    *   **Contextual Memory:** Utilizes user-specific conversation history to inform responses.
    *   **Usage Limits:** While not explicitly detailed in the provided code snippets, token tracking suggests potential future implementation of usage limits or cost management.

### `!問 <內容>`
*   **Purpose:** Provides quick and concise answers to user questions. Optimized for information retrieval and shorter responses compared to `!推理`.
*   **Input:** `<內容>` - Text content representing the user's question. Attached PDF files or images are also processed.
*   **Actions:**
    *   Processes input text and any attached PDF or image files, similar to `!推理`.
    *   Primarily uses Perplexity AI (via `pplx_chat_completion`) for its strength in providing direct answers.
    *   If Perplexity AI is unavailable or encounters an error, it falls back to OpenAI's GPT-3.5 Turbo (`gpt-3.5-turbo-1106`). If image data is present and Perplexity fails, it attempts to use Google Gemini Pro (`gemini-pro`).
    *   Retrieves relevant past conversation snippets from user memory.
    *   Generates a response.
    *   Stores the interaction in user memory.
    *   Tracks token usage for OpenAI calls (if used as a fallback) and updates user statistics.
*   **Specific Behaviors:**
    *   **PDF/Image Processing:** Similar to `!推理`.
    *   **Primary AI:** Favors Perplexity AI for its intended purpose.
    *   **Fallback Mechanism:** Switches to OpenAI or Gemini models if Perplexity fails or if image data requires multimodal processing.
    *   **Token Tracking:** Tracks tokens for OpenAI calls.
    *   **Contextual Memory:** Uses conversation history.

### `!整理 <來源頻道/討論串ID> <摘要送出頻道ID>`
*   **Purpose:** Summarizes the conversation from a specified source channel or thread and sends the summary to a target channel.
*   **Input:**
    *   `<來源頻道/討論串ID>`: The ID of the Discord channel or thread from which to fetch messages for summarization.
    *   `<摘要送出頻道ID>`: The ID of the Discord channel where the generated summary should be sent.
*   **Actions:**
    *   Fetches message history from the source channel/thread. It attempts to retrieve a large number of messages (up to 3000, though Discord limits might apply per request).
    *   Filters messages to exclude bot commands and messages from other bots.
    *   Concatenates the fetched messages into a single text block.
    *   Calls an AI model (likely OpenAI's GPT-3.5 Turbo due to the potentially large amount of text, though the specific model for summarization isn't explicitly locked to `gpt-3.5-turbo-1106` in the `summarize_channel_messages` function call, it defaults to it) to generate a summary of the conversation.
    *   Sends the generated summary to the specified target channel.
    *   Tracks token usage for the summarization AI call and updates the user's (who invoked the command) usage statistics.
*   **Specific Behaviors:**
    *   **Message Fetching:** Retrieves a history of messages.
    *   **Message Filtering:** Cleans the message list by removing bot commands and other bot messages.
    *   **Large Text Handling:** Designed to process a significant amount of conversational data for summarization. The code attempts to summarize in chunks if the content is too large for a single API call.
    *   **Token Tracking:** Monitors and records token usage for the summarization task.

### `!搜尋 <查詢內容>`
*   **Purpose:** Performs a web search using Google and provides a summary of the findings, potentially with links to the sources.
*   **Input:** `<查詢內容>` - The text query to search for on the web.
*   **Actions:**
    *   Uses the Google Custom Search API (`googleapiclient.discovery.build("customsearch", "v1")`) to search the web for the query.
    *   Retrieves search results, likely focusing on the top few results.
    *   Processes the content of the retrieved web pages (details of how deep this processing goes, e.g., fetching full page content vs. using snippets, are not fully clear from the snippets but it does fetch content from URLs).
    *   Uses an AI model (likely OpenAI's GPT-3.5 Turbo, as specified in `fetch_and_summarize_web_content`) to summarize the gathered information from the search results.
    *   Presents the summarized information to the user, possibly including links to the original sources.
    *   Tracks token usage for the summarization AI call and updates the user's usage statistics.
*   **Specific Behaviors:**
    *   **Web Search:** Integrates with Google Search.
    *   **Content Summarization:** Uses AI to summarize web content.
    *   **Token Tracking:** Monitors and records token usage.

### `!重置記憶`
*   **Purpose:** Initiates the process of resetting the user's conversational memory. This acts as a confirmation step before actual deletion.
*   **Input:** None.
*   **Actions:**
    *   Checks if there's an existing memory reset confirmation pending for the user.
    *   If not, it sets a state for the user indicating that a memory reset is pending confirmation (e.g., by adding the user's ID to a `confirm_reset_users` set).
    *   Sends a message to the user asking them to confirm the reset using `!確定重置` or cancel using `!取消重置`.
*   **Specific Behaviors:**
    *   **Confirmation Step:** Does not delete memory immediately but requires a second command to confirm.
    *   **State Management:** Temporarily stores the user's intent to reset.

### `!確定重置`
*   **Purpose:** Confirms the user's intention to reset their conversational memory, leading to the deletion of their stored history.
*   **Input:** None.
*   **Actions:**
    *   Checks if the user is in the `confirm_reset_users` set (i.e., if `!重置記憶` was used beforehand).
    *   If confirmed, it deletes all conversation history associated with the user ID from the PostgreSQL database (from the `user_memory` table).
    *   Removes the user from the `confirm_reset_users` set.
    *   Sends a confirmation message to the user that their memory has been reset.
*   **Specific Behaviors:**
    *   **Data Deletion:** Performs the actual deletion of user-specific data from the database.
    *   **Requires Prior Step:** Only works if `!重置記憶` was invoked first.

### `!取消重置`
*   **Purpose:** Cancels a pending request to reset the user's conversational memory.
*   **Input:** None.
*   **Actions:**
    *   Checks if the user is in the `confirm_reset_users` set.
    *   If yes, it removes the user from the `confirm_reset_users` set, effectively canceling the reset request.
    *   Sends a message to the user confirming that the memory reset has been canceled.
*   **Specific Behaviors:**
    *   **State Management:** Reverts the user's intent to reset.

### `!顯示記憶`
*   **Purpose:** Allows the user to view their stored conversational memory.
*   **Input:** None.
*   **Actions:**
    *   Fetches the user's conversation history from the PostgreSQL database (`user_memory` table).
    *   Formats the retrieved memory (which includes past user inputs and AI responses) into a readable string.
    *   Sends the formatted memory back to the user. If the memory is too long for a single Discord message, it might be truncated or sent in parts (the code shows it being sent as a single block, which could hit Discord's message length limit).
*   **Specific Behaviors:**
    *   **Data Retrieval:** Fetches data from the PostgreSQL database.
    *   **Output Formatting:** Presents the memory in a user-friendly way.

### `!指令選單`
*   **Purpose:** Displays a help message listing available commands and their basic usage instructions.
*   **Input:** None.
*   **Actions:**
    *   Constructs a message containing a list of all available user commands and a brief description of how to use them (e.g., expected arguments).
    *   Sends this help message to the user.
*   **Specific Behaviors:**
    *   **Help Information:** Provides users with a quick reference for bot commands.

## Technical Details

### Key Dependencies
The project relies on several key Python libraries:
*   **`discord.py`**: For interacting with the Discord API, handling bot events, and managing commands.
*   **`openai`**: The official OpenAI Python client for accessing GPT models (e.g., GPT-4 Turbo, GPT-3.5 Turbo).
*   **`perplexity-ai`**: The official Perplexity AI Python client for the `pplx-chat-completion` models.
*   **`google-generativeai`**: For using Google Gemini models.
*   **`google-api-python-client`**: For interacting with Google APIs, specifically used here for the Google Custom Search API in the `!搜尋` command.
*   **`psycopg2-binary`**: A PostgreSQL adapter for Python, used to connect to and interact with the PostgreSQL database for storing user memory and usage statistics.
*   **`tiktoken`**: Used for counting tokens for OpenAI models, helping to manage context window limits and track usage.
*   **`python-dotenv`**: For managing environment variables by loading them from a `.env` file.
*   **`PyPDF2`**: For reading and extracting text content from PDF files attached to messages.
*   **`Pillow`**: The Python Imaging Library (Fork), used for processing and handling image attachments.
*   **`aiohttp`**: Asynchronous HTTP client/server framework, often a dependency of libraries like `discord.py` for handling web requests.
*   **`asyncpg`**: An asynchronous PostgreSQL driver, likely used by helper functions for database interaction in an async environment.

### Environment Variables
The bot requires the following environment variables to be set for full functionality:
*   `DISCORD_TOKEN`: The authentication token for the Discord bot.
*   `OPENAI_API_KEY`: API key for OpenAI services.
*   `PPLX_API_KEY`: API key for Perplexity AI services.
*   `GEMINI_API_KEY`: API key for Google Gemini services.
*   `GOOGLE_API_KEY`: API key for Google Cloud services, including the Custom Search API.
*   `GOOGLE_CSE_ID`: The Programmable Search Engine ID for Google Custom Search.
*   `POSTGRES_HOST`: Hostname or IP address of the PostgreSQL server.
*   `POSTGRES_PORT`: Port number for the PostgreSQL server (default is usually 5432).
*   `POSTGRES_DB`: Name of the PostgreSQL database.
*   `POSTGRES_USER`: Username for connecting to the PostgreSQL database.
*   `POSTGRES_PASSWORD`: Password for the PostgreSQL database user.

### Database Schema
The bot utilizes a PostgreSQL database with at least two main tables:

1.  **`user_memory` Table:** Stores the history of interactions for each user to provide conversational context.
    *   `user_id` (BIGINT, Primary Key): The Discord user ID.
    *   `timestamp` (TIMESTAMP WITH TIME ZONE, Primary Key): The timestamp of the message.
    *   `message_type` (TEXT): Indicates if the message is from the 'user' or the 'assistant' (bot).
    *   `content` (TEXT): The textual content of the message.
    *   *(Implicitly, there might be an ordering by timestamp to retrieve conversation history correctly.)*

2.  **`feature_usage` Table:** Tracks the usage of different bot features and associated token counts for each user.
    *   `user_id` (BIGINT, Primary Key): The Discord user ID.
    *   `feature_name` (TEXT, Primary Key): The name of the feature/command being used (e.g., "推理", "問", "整理", "搜尋").
    *   `usage_count` (INTEGER): The number of times the user has invoked this feature.
    *   `tokens_used` (INTEGER): The cumulative number of tokens processed by AI models for this feature by this user (primarily for OpenAI models).

Database initialization (`init_db` function) ensures these tables are created if they don't exist.

### Other Technical Aspects

*   **Error Handling:**
    *   The bot employs `try-except` blocks extensively, especially around API calls (OpenAI, Perplexity, Gemini, Google Search) and I/O operations (file processing, database interactions).
    *   Specific exceptions (e.g., `openai.APIError`, `perplexity.APIError`, `google.api_core.exceptions`, `discord.HTTPException`) are caught.
    *   Users are typically notified with an error message in Discord if a command fails (e.g., "處理您的請求時發生錯誤," "模型處理錯誤," "無法從PDF提取文字").
    *   Logging (`logging` module) is used to record errors and informational messages, which aids in debugging and monitoring.

*   **Token Counting (`tiktoken`):**
    *   The `tiktoken` library is used to count the number of tokens an input text will consume for OpenAI models. This is crucial for:
        *   Ensuring the input + context does not exceed the model's maximum token limit.
        *   Managing costs by tracking token usage per user and per feature.
        *   Potentially truncating or summarizing text if it's too long.
    *   The `count_tokens` function is a utility for this purpose.

*   **Model/Service Switching Logic:**
    *   **`!推理` command:** Prioritizes OpenAI GPT-4 Turbo. If the input is too large, it may switch to Gemini Pro (if images are present) or GPT-3.5 Turbo.
    *   **`!問` command:** Primarily uses Perplexity AI. If Perplexity fails or if image input is provided, it falls back to Gemini Pro (for images) or OpenAI GPT-3.5 Turbo.
    *   **`!整理` command:** Uses an OpenAI model (defaults to GPT-3.5 Turbo) for summarizing channel messages. It includes logic to handle large conversations by potentially chunking and summarizing parts if they exceed token limits.
    *   **`!搜尋` command:** Uses the Google Custom Search API to fetch web search results. The content from these results is then summarized using an AI model (typically OpenAI GPT-3.5 Turbo).
    *   **Multimodal Input (Images/PDFs):** For commands like `!推理` and `!問`, if image attachments are present, the bot attempts to use Google Gemini Pro for its multimodal capabilities. PDFs are processed to extract text, which is then used as input for the respective text-based AI models.

*   **Asynchronous Operations:** The bot is built using `async` and `await` keywords, making heavy use of Python's `asyncio` capabilities. This is essential for a Discord bot to handle multiple concurrent users and I/O-bound operations (API calls, database queries) efficiently without blocking.

## Overall Functional Summary

This project implements a sophisticated Discord bot designed to serve as an AI-powered assistant, enhancing server engagement and information access. At its core, the bot leverages a suite of advanced AI models from OpenAI (GPT-4 Turbo, GPT-3.5 Turbo), Perplexity AI, and Google Gemini to offer a diverse range of functionalities.

Key capabilities include:
*   **Advanced Reasoning and Chat (`!推理`):** Provides in-depth responses and engages in complex discussions, utilizing powerful models like GPT-4 Turbo. Supports multimodal input by processing text from attached PDF files and understanding content from images (via Gemini).
*   **Multimodal Question Answering (`!問`):** Delivers concise answers to user queries, primarily through Perplexity AI, with fallbacks to OpenAI and Gemini for broader coverage and multimodal (image/PDF) input processing.
*   **Channel Summarization (`!整理`):** Fetches and condenses conversations from specified Discord channels or threads, providing users with quick digests of lengthy discussions.
*   **Web Search and Summarization (`!搜尋`):** Performs Google web searches based on user queries and uses AI to summarize the findings, offering a streamlined way to gather and understand online information.

To ensure personalized and contextually aware interactions, the bot maintains a user-specific conversational memory in a PostgreSQL database. This allows for more coherent follow-up questions and responses. The database also tracks feature usage and AI model token consumption for each user, which is vital for monitoring and potentially managing resource allocation.

The bot is built with robust error handling, asynchronous operations for efficiency, and relies on a defined set of environment variables and Python dependencies for its operation. Users can manage their conversational memory with commands to display or reset it, and a help command (`!指令選單`) is available for easy discovery of the bot's features.
